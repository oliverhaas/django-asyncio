"""Native async prefetch_related() on PostgreSQL, run outside async_to_sync."""

import asyncio
import time
import unittest

from asgiref import sync as asgiref_sync

from django.contrib.contenttypes.models import ContentType
from django.db import (
    DEFAULT_DB_ALIAS,
    ProgrammingError,
    connection,
    connections,
    transaction,
)
from django.db.models import (
    Prefetch,
    aprefetch_related_objects,
    prefetch_related_objects,
)
from django.db.models.functions import Length
from django.test import TransactionTestCase

from .models import (
    Author,
    AuthorAddress,
    Book,
    Bookmark,
    Department,
    House,
    Person,
    Qualification,
    Reader,
    Room,
    TaggedItem,
    Teacher,
)


def run_native(coro_func):
    """Run coro_func() outside async_to_sync; return its result and the
    number of sync_to_async calls."""
    calls = []
    original = asgiref_sync.SyncToAsync.__call__

    async def tracking(self, *args, **kwargs):
        calls.append(True)
        return await original(self, *args, **kwargs)

    async def wrapper():
        try:
            return await coro_func()
        finally:
            await connection.aclose()

    asgiref_sync.SyncToAsync.__call__ = tracking
    try:
        result = asyncio.run(wrapper())
    finally:
        asgiref_sync.SyncToAsync.__call__ = original
    return result, len(calls)


def names(objs):
    return [obj.name for obj in objs]


def walk_books_read_by(authors):
    return [
        (
            author.name,
            author.first_book.title,
            names(author.first_book.read_by.all()),
            [(book.title, names(book.read_by.all())) for book in author.books.all()],
        )
        for author in authors
    ]


def walk_main_room_of(rooms):
    houses = [getattr(room, "main_room_of", None) for room in rooms]
    return [
        (room.name, house and house.name, house and house.main_room.name)
        for room, house in zip(rooms, houses)
    ]


def walk_readers(readers):
    return [
        (
            reader.name,
            [
                (
                    book.title,
                    [
                        (author.name, [a.address for a in author.addresses.all()])
                        for author in book.authors.all()
                    ],
                )
                for book in reader.books_read.all()
            ],
        )
        for reader in readers
    ]


def walk_rooms(houses):
    return [
        (house.name, [(room.name, room.house.name) for room in house.rooms.all()])
        for house in houses
    ]


def walk_teachers(departments):
    return [
        (
            department.name,
            [
                (teacher.name, names(teacher.qualifications.all()))
                for teacher in department.teachers.all()
            ],
        )
        for department in departments
    ]


BOOKS_READ_BY = [
    ("Charlotte", "Poems", ["Amy"], [("Poems", ["Amy"]), ("Jane Eyre", ["Belinda"])]),
    ("Anne", "Poems", ["Amy"], [("Poems", ["Amy"])]),
    ("Emily", "Poems", ["Amy"], [("Poems", ["Amy"]), ("Wuthering Heights", [])]),
    (
        "Jane",
        "Sense and Sensibility",
        ["Amy", "Belinda"],
        [("Sense and Sensibility", ["Amy", "Belinda"])],
    ),
]

HOUSE_ROOMS = [
    ("House 1", [("House 1 kitchen", "House 1"), ("House 1 hall", "House 1")]),
    ("House 2", [("House 2 kitchen", "House 2")]),
    ("House 3", []),
]

PARITY_CASES = [
    (
        "forward_fk",
        lambda: Author.objects.prefetch_related("first_book"),
        lambda authors: [(a.name, a.first_book.title) for a in authors],
        [
            ("Charlotte", "Poems"),
            ("Anne", "Poems"),
            ("Emily", "Poems"),
            ("Jane", "Sense and Sensibility"),
        ],
    ),
    (
        "forward_one_to_one",
        lambda: House.objects.prefetch_related("main_room"),
        lambda houses: [
            (
                h.name,
                h.main_room and h.main_room.name,
                h.main_room and h.main_room.main_room_of.name,
            )
            for h in houses
        ],
        [
            ("House 1", "House 1 kitchen", "House 1"),
            ("House 2", "House 2 kitchen", "House 2"),
            ("House 3", None, None),
        ],
    ),
    (
        "reverse_one_to_one",
        lambda: Room.objects.prefetch_related("main_room_of"),
        walk_main_room_of,
        [
            ("House 1 kitchen", "House 1", "House 1 kitchen"),
            ("House 1 hall", None, None),
            ("House 2 kitchen", "House 2", "House 2 kitchen"),
        ],
    ),
    (
        "reverse_fk",
        lambda: House.objects.prefetch_related("rooms"),
        walk_rooms,
        HOUSE_ROOMS,
    ),
    (
        "m2m_forward",
        lambda: Book.objects.prefetch_related("authors"),
        lambda books: [(b.title, names(b.authors.all())) for b in books],
        [
            ("Poems", ["Charlotte", "Anne", "Emily"]),
            ("Jane Eyre", ["Charlotte"]),
            ("Wuthering Heights", ["Emily"]),
            ("Sense and Sensibility", ["Jane"]),
        ],
    ),
    (
        "m2m_reverse",
        lambda: Author.objects.prefetch_related("books"),
        lambda authors: [(a.name, [b.title for b in a.books.all()]) for a in authors],
        [
            ("Charlotte", ["Poems", "Jane Eyre"]),
            ("Anne", ["Poems"]),
            ("Emily", ["Poems", "Wuthering Heights"]),
            ("Jane", ["Sense and Sensibility"]),
        ],
    ),
    (
        "generic_relation",
        lambda: Bookmark.objects.prefetch_related("tags"),
        lambda bookmarks: [(b.url, [t.tag for t in b.tags.all()]) for b in bookmarks],
        [("http://a.example", ["django", "python"]), ("http://b.example", [])],
    ),
    (
        "three_levels",
        lambda: Reader.objects.prefetch_related("books_read__authors__addresses"),
        walk_readers,
        [
            (
                "Amy",
                [
                    (
                        "Poems",
                        [("Charlotte", ["Haworth"]), ("Anne", []), ("Emily", [])],
                    ),
                    ("Sense and Sensibility", [("Jane", [])]),
                ],
            ),
            (
                "Belinda",
                [
                    ("Jane Eyre", [("Charlotte", ["Haworth"])]),
                    ("Sense and Sensibility", [("Jane", [])]),
                ],
            ),
        ],
    ),
    (
        "two_levels_two_branches",
        lambda: Author.objects.prefetch_related(
            "books", "first_book", "books__read_by", "first_book__read_by"
        ),
        walk_books_read_by,
        BOOKS_READ_BY,
    ),
    (
        "walk_cached",
        lambda: Author.objects.select_related("first_book").prefetch_related(
            "first_book__read_by"
        ),
        lambda authors: [
            (a.name, a.first_book.title, names(a.first_book.read_by.all()))
            for a in authors
        ],
        [
            ("Charlotte", "Poems", ["Amy"]),
            ("Anne", "Poems", ["Amy"]),
            ("Emily", "Poems", ["Amy"]),
            ("Jane", "Sense and Sensibility", ["Amy", "Belinda"]),
        ],
    ),
    (
        "to_attr",
        lambda: Author.objects.prefetch_related(
            Prefetch(
                "books",
                queryset=Book.objects.filter(title__in=["Poems", "Wuthering Heights"]),
                to_attr="selected_books",
            )
        ),
        lambda authors: [
            (a.name, [b.title for b in a.selected_books]) for a in authors
        ],
        [
            ("Charlotte", ["Poems"]),
            ("Anne", ["Poems"]),
            ("Emily", ["Poems", "Wuthering Heights"]),
            ("Jane", []),
        ],
    ),
    (
        "queryset_with_own_prefetch",
        lambda: Author.objects.prefetch_related(
            Prefetch("books", queryset=Book.objects.prefetch_related("read_by"))
        ),
        lambda authors: [
            (a.name, [(b.title, names(b.read_by.all())) for b in a.books.all()])
            for a in authors
        ],
        [
            ("Charlotte", [("Poems", ["Amy"]), ("Jane Eyre", ["Belinda"])]),
            ("Anne", [("Poems", ["Amy"])]),
            ("Emily", [("Poems", ["Amy"]), ("Wuthering Heights", [])]),
            ("Jane", [("Sense and Sensibility", ["Amy", "Belinda"])]),
        ],
    ),
    (
        "default_manager_prefetch",
        lambda: Department.objects.prefetch_related("teachers"),
        walk_teachers,
        [
            ("Maths", [("Ann", ["BA", "MSc"]), ("Bob", ["BA"])]),
            ("Art", [("Bob", ["BA"])]),
        ],
    ),
    (
        "auto_and_user_lookup_overlap",
        lambda: Department.objects.prefetch_related(
            "teachers", "teachers__qualifications"
        ),
        walk_teachers,
        [
            ("Maths", [("Ann", ["BA", "MSc"]), ("Bob", ["BA"])]),
            ("Art", [("Bob", ["BA"])]),
        ],
    ),
    (
        "custom_iterable",
        lambda: Department.objects.prefetch_related(
            Prefetch("teachers", queryset=Teacher.objects_custom.all())
        ),
        lambda departments: [(d.name, names(d.teachers.all())) for d in departments],
        [("Maths", ["Ann", "Bob"]), ("Art", ["Bob"])],
    ),
    (
        "annotate_and_select_related",
        lambda: House.objects.prefetch_related(
            Prefetch(
                "rooms",
                queryset=Room.objects.select_related("house").annotate(
                    name_length=Length("name")
                ),
            )
        ),
        lambda houses: [
            (h.name, [(r.name, r.house.name, r.name_length) for r in h.rooms.all()])
            for h in houses
        ],
        [
            (
                "House 1",
                [("House 1 kitchen", "House 1", 15), ("House 1 hall", "House 1", 12)],
            ),
            ("House 2", [("House 2 kitchen", "House 2", 15)]),
            ("House 3", []),
        ],
    ),
    (
        "deferred_fields",
        lambda: House.objects.prefetch_related(
            Prefetch("rooms", queryset=Room.objects.only("name", "house"))
        ),
        walk_rooms,
        HOUSE_ROOMS,
    ),
    (
        "empty_result",
        lambda: House.objects.prefetch_related(
            Prefetch("rooms", queryset=Room.objects.filter(name="nothing"))
        ),
        walk_rooms,
        [("House 1", []), ("House 2", []), ("House 3", [])],
    ),
    (
        "empty_result_set",
        lambda: House.objects.prefetch_related(
            Prefetch("rooms", queryset=Room.objects.none())
        ),
        walk_rooms,
        [("House 1", []), ("House 2", []), ("House 3", [])],
    ),
]


@unittest.skipUnless(
    connection.vendor == "postgresql",
    "Native async execution path is currently postgresql-only.",
)
class NativeAsyncPrefetchTests(TransactionTestCase):
    available_apps = ["django.contrib.contenttypes", "prefetch_related"]
    databases = {"default", "other"}

    def setUp(self):
        poems = Book.objects.create(title="Poems")
        jane_eyre = Book.objects.create(title="Jane Eyre")
        wuthering = Book.objects.create(title="Wuthering Heights")
        sense = Book.objects.create(title="Sense and Sensibility")
        charlotte = Author.objects.create(name="Charlotte", first_book=poems)
        anne = Author.objects.create(name="Anne", first_book=poems)
        emily = Author.objects.create(name="Emily", first_book=poems)
        jane = Author.objects.create(name="Jane", first_book=sense)
        poems.authors.add(charlotte, anne, emily)
        jane_eyre.authors.add(charlotte)
        wuthering.authors.add(emily)
        sense.authors.add(jane)
        AuthorAddress.objects.create(author=charlotte, address="Haworth")
        amy = Reader.objects.create(name="Amy")
        belinda = Reader.objects.create(name="Belinda")
        amy.books_read.add(poems, sense)
        belinda.books_read.add(jane_eyre, sense)

        joe = Person.objects.create(name="Joe")
        mary = Person.objects.create(name="Mary")
        house1 = House.objects.create(name="House 1", address="1 Main St", owner=joe)
        house2 = House.objects.create(name="House 2", address="2 Main St", owner=mary)
        House.objects.create(name="House 3", address="3 Main St")
        house1.main_room = Room.objects.create(name="House 1 kitchen", house=house1)
        house1.save()
        Room.objects.create(name="House 1 hall", house=house1)
        house2.main_room = Room.objects.create(name="House 2 kitchen", house=house2)
        house2.save()
        joe.houses.add(house1, house2)
        mary.houses.add(house2)

        ba = Qualification.objects.create(name="BA")
        msc = Qualification.objects.create(name="MSc")
        ann = Teacher.objects.create(name="Ann")
        bob = Teacher.objects.create(name="Bob")
        ann.qualifications.add(ba, msc)
        bob.qualifications.add(ba)
        Department.objects.create(name="Maths").teachers.add(ann, bob)
        Department.objects.create(name="Art").teachers.add(bob)

        tagged = Bookmark.objects.create(url="http://a.example")
        Bookmark.objects.create(url="http://b.example")
        TaggedItem.objects.create(tag="django", content_object=tagged)
        TaggedItem.objects.create(tag="python", content_object=tagged)
        ContentType.objects.get_for_model(Bookmark)

    def assertParity(self, sync_func, async_func):
        """Assert that the native path returns the sync result; return it."""
        expected = sync_func()
        result, s2a = run_native(async_func)
        self.assertEqual(result, expected)
        self.assertEqual(s2a, 0)
        return result

    def assertPrefetchParity(self, make_queryset, walk):
        """Walk the native result in the loop, where a cache miss raises."""

        async def native():
            return walk([obj async for obj in make_queryset()])

        return self.assertParity(lambda: walk(list(make_queryset())), native)

    def test_parity_with_sync(self):
        for name, make_queryset, walk, expected in PARITY_CASES:
            with self.subTest(name):
                result = self.assertPrefetchParity(make_queryset, walk)
                self.assertEqual(result, expected)

    def test_partially_prefetched_instances(self):
        def walk(authors):
            return [(a.name, [b.title for b in a.books.all()]) for a in authors]

        def sync_func():
            authors = list(Author.objects.all())
            prefetch_related_objects(authors[:2], "books")
            prefetch_related_objects(authors, "books")
            return walk(authors)

        async def async_func():
            authors = [a async for a in Author.objects.all()]
            await aprefetch_related_objects(authors[:2], "books")
            await aprefetch_related_objects(authors, "books")
            return walk(authors)

        result = self.assertParity(sync_func, async_func)
        self.assertEqual(result[2], ("Emily", ["Poems", "Wuthering Heights"]))

    def test_inside_atomic_sees_uncommitted_rows(self):
        async def body():
            async with transaction.atomic():
                zoe = await Person.objects.acreate(name="Zoe")
                house = await House.objects.acreate(
                    name="House 4", address="4 Main St", owner=zoe
                )
                await Room.objects.acreate(name="House 4 attic", house=house)
                houses = [
                    h
                    async for h in House.objects.filter(
                        name="House 4"
                    ).prefetch_related("owner", "rooms")
                ]
                return [(h.name, h.owner.name, names(h.rooms.all())) for h in houses]

        result, s2a = run_native(body)
        self.assertEqual(result, [("House 4", "Zoe", ["House 4 attic"])])
        self.assertEqual(s2a, 0)

    def test_failing_query_fails_the_prefetch(self):
        broken = Room.objects.extra(where=["no_such_column = 1"])

        async def body():
            with self.assertRaises(ProgrammingError):
                async for _ in House.objects.prefetch_related(
                    "owner", Prefetch("rooms", queryset=broken)
                ):
                    pass
            return await House.objects.acount()

        count, s2a = run_native(body)
        self.assertEqual(count, 3)
        self.assertEqual(s2a, 0)

    def test_failing_prefetch_inside_atomic_rolls_back(self):
        broken = Room.objects.extra(where=["no_such_column = 1"])

        async def body():
            with self.assertRaises(ProgrammingError):
                async with transaction.atomic():
                    await Person.objects.acreate(name="Zoe")
                    async for _ in House.objects.prefetch_related(
                        "owner", Prefetch("rooms", queryset=broken)
                    ):
                        pass
            return (
                await Person.objects.filter(name="Zoe").aexists(),
                await House.objects.acount(),
            )

        (zoe_exists, count), s2a = run_native(body)
        self.assertIs(zoe_exists, False)
        self.assertEqual(count, 3)
        self.assertEqual(s2a, 0)

    def test_concurrent_tasks_share_the_connection(self):
        def expected():
            return (
                walk_books_read_by(
                    Author.objects.prefetch_related(
                        "books", "first_book", "books__read_by", "first_book__read_by"
                    )
                ),
                walk_teachers(Department.objects.prefetch_related("teachers")),
                House.objects.count(),
            )

        async def authors():
            return walk_books_read_by(
                [
                    a
                    async for a in Author.objects.prefetch_related(
                        "books", "first_book", "books__read_by", "first_book__read_by"
                    )
                ]
            )

        async def departments():
            return walk_teachers(
                [d async for d in Department.objects.prefetch_related("teachers")]
            )

        async def body():
            await connection.aensure_connection()
            shared = connection.async_connection
            results = await asyncio.gather(
                authors(), departments(), House.objects.acount()
            )
            return tuple(results), connection.async_connection is shared

        (results, shared), s2a = run_native(body)
        self.assertEqual(results, expected())
        self.assertIs(shared, True)
        self.assertEqual(s2a, 0)

    def _cancel_slow_prefetch(self):
        """Return a coroutine function that cancels a slow prefetch."""
        slow = Room.objects.extra(where=["(SELECT 1 FROM pg_sleep(5)) = 1"])

        async def prefetch():
            return [
                h
                async for h in House.objects.prefetch_related(
                    "owner", Prefetch("rooms", queryset=slow)
                )
            ]

        async def cancel():
            await connection.aensure_connection()
            task = asyncio.ensure_future(prefetch())
            await asyncio.sleep(0.5)
            start = time.monotonic()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return time.monotonic() - start

        return cancel

    def test_cancel_during_prefetch(self):
        cancel = self._cancel_slow_prefetch()

        async def body():
            elapsed = await cancel()
            return elapsed, await House.objects.acount()

        (elapsed, count), s2a = run_native(body)
        self.assertLess(elapsed, 4)
        self.assertEqual(count, 3)
        self.assertEqual(s2a, 0)

    def test_cancel_during_prefetch_pooled(self):
        options = {
            **connection.settings_dict["OPTIONS"],
            "pool": {"min_size": 1, "max_size": 2},
        }
        pooled = connections[DEFAULT_DB_ALIAS].__class__(
            {**connection.settings_dict, "OPTIONS": options}, alias=DEFAULT_DB_ALIAS
        )
        cancel = self._cancel_slow_prefetch()

        async def body():
            original = connections[DEFAULT_DB_ALIAS]
            setattr(connections._connections, DEFAULT_DB_ALIAS, pooled)
            try:
                elapsed = await cancel()
                await pooled.aclose()
                return elapsed, await House.objects.acount()
            finally:
                await pooled.aclose()
                await pooled.aclose_pool()
                setattr(connections._connections, DEFAULT_DB_ALIAS, original)

        (elapsed, count), s2a = run_native(body)
        self.assertLess(elapsed, 4)
        self.assertEqual(count, 3)
        self.assertEqual(s2a, 0)
