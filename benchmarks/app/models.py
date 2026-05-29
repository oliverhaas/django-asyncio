from django.db import models


class Widget(models.Model):
    """Trivial model for the single-row DB-bound benchmark scenario."""

    name = models.CharField(max_length=100)
    value = models.IntegerField(default=0)


# ---------------------------------------------------------------------------
# Heavy-prefetch schema for the db_heavy scenario.
#
# Root model is Author. A page of authors is fetched and ~16 relations are
# prefetched, spanning every relation kind and mixing flat (direct off Author)
# with nested (relation-of-a-relation) lookups. Each lookup is one query, so
# under per-query network latency the sequential cost grows with the number of
# lookups while the parallel cost grows only with the depth of the tree.
# ---------------------------------------------------------------------------


class Publisher(models.Model):
    name = models.CharField(max_length=100)


class Genre(models.Model):
    name = models.CharField(max_length=100)


class Category(models.Model):
    name = models.CharField(max_length=100)


class Reviewer(models.Model):
    name = models.CharField(max_length=100)


class Agent(models.Model):
    name = models.CharField(max_length=100)


class Follower(models.Model):
    name = models.CharField(max_length=100)


class Tag(models.Model):
    name = models.CharField(max_length=100)


class Author(models.Model):
    name = models.CharField(max_length=100)
    # Flat M2M relations off the root.
    followers = models.ManyToManyField(Follower, related_name="authors")
    tags = models.ManyToManyField(Tag, related_name="authors")


class Book(models.Model):
    title = models.CharField(max_length=200)
    publisher = models.ForeignKey(
        Publisher, models.CASCADE, related_name="books"
    )  # nested forward FK: books__publisher
    authors = models.ManyToManyField(Author, related_name="books")  # flat M2M
    genres = models.ManyToManyField(
        Genre, related_name="books"
    )  # nested M2M: books__genres


class Review(models.Model):
    book = models.ForeignKey(
        Book, models.CASCADE, related_name="reviews"
    )  # nested reverse FK: books__reviews
    reviewer = models.ForeignKey(
        Reviewer, models.CASCADE, related_name="reviews"
    )  # deep nested forward FK: books__reviews__reviewer
    rating = models.IntegerField(default=0)


class Article(models.Model):
    author = models.ForeignKey(
        Author, models.CASCADE, related_name="articles"
    )  # flat reverse FK
    category = models.ForeignKey(
        Category, models.CASCADE, related_name="articles"
    )  # nested forward FK: articles__category
    title = models.CharField(max_length=200)


class Comment(models.Model):
    article = models.ForeignKey(
        Article, models.CASCADE, related_name="comments"
    )  # nested reverse FK: articles__comments
    body = models.CharField(max_length=200)


class Award(models.Model):
    author = models.ForeignKey(
        Author, models.CASCADE, related_name="awards"
    )  # flat reverse FK
    name = models.CharField(max_length=100)


class Profile(models.Model):
    author = models.OneToOneField(
        Author, models.CASCADE, related_name="profile"
    )  # flat reverse O2O
    bio = models.TextField(default="")


class Avatar(models.Model):
    profile = models.OneToOneField(
        Profile, models.CASCADE, related_name="avatar"
    )  # nested reverse O2O: profile__avatar
    url = models.CharField(max_length=200)


class Address(models.Model):
    author = models.ForeignKey(
        Author, models.CASCADE, related_name="addresses"
    )  # flat reverse FK
    city = models.CharField(max_length=100)


class Contract(models.Model):
    author = models.ForeignKey(
        Author, models.CASCADE, related_name="contracts"
    )  # flat reverse FK
    agent = models.ForeignKey(
        Agent, models.CASCADE, related_name="contracts"
    )  # nested forward FK: contracts__agent
    amount = models.IntegerField(default=0)


# The lookups passed to prefetch_related() in the db_heavy views. Kept here so
# the view, the seed, and the validation script agree on one definition.
HEAVY_PREFETCH_LOOKUPS = [
    "books",
    "books__publisher",
    "books__genres",
    "books__reviews",
    "books__reviews__reviewer",
    "articles",
    "articles__category",
    "articles__comments",
    "awards",
    "profile",
    "profile__avatar",
    "followers",
    "tags",
    "addresses",
    "contracts",
    "contracts__agent",
]
