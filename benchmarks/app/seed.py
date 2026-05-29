"""Deterministic seed for the db_heavy benchmark schema.

Builds a fixed graph of authors with every relation populated, sharing
lookup rows (publishers, genres, ...) across authors. Idempotent: clears the
heavy tables first.
"""

from . import models as m

HEAVY_MODELS = [
    m.Avatar,
    m.Profile,
    m.Comment,
    m.Article,
    m.Review,
    m.Award,
    m.Address,
    m.Contract,
    m.Book,
    m.Author,
    m.Publisher,
    m.Genre,
    m.Category,
    m.Reviewer,
    m.Agent,
    m.Follower,
    m.Tag,
]


def clear_heavy():
    for model in HEAVY_MODELS:
        model.objects.all().delete()


def seed_heavy(n_authors=20):
    """Create n_authors, each wired to the full relation graph."""
    clear_heavy()

    publishers = [m.Publisher.objects.create(name=f"Publisher {i}") for i in range(5)]
    genres = [m.Genre.objects.create(name=f"Genre {i}") for i in range(6)]
    categories = [m.Category.objects.create(name=f"Category {i}") for i in range(4)]
    reviewers = [m.Reviewer.objects.create(name=f"Reviewer {i}") for i in range(8)]
    agents = [m.Agent.objects.create(name=f"Agent {i}") for i in range(5)]
    followers = [m.Follower.objects.create(name=f"Follower {i}") for i in range(10)]
    tags = [m.Tag.objects.create(name=f"Tag {i}") for i in range(8)]

    for a in range(n_authors):
        author = m.Author.objects.create(name=f"Author {a}")
        author.followers.set(followers[a % 5 : a % 5 + 3])
        author.tags.set(tags[a % 4 : a % 4 + 2])

        m.Profile.objects.create(author=author, bio=f"Bio of author {a}")
        m.Avatar.objects.create(profile=author.profile, url=f"http://av/{a}.png")

        for j in range(2):
            m.Award.objects.create(author=author, name=f"Award {a}-{j}")
            m.Address.objects.create(author=author, city=f"City {a}-{j}")
            m.Contract.objects.create(
                author=author, agent=agents[(a + j) % 5], amount=100 * a + j
            )
            article = m.Article.objects.create(
                author=author,
                category=categories[(a + j) % 4],
                title=f"Article {a}-{j}",
            )
            for k in range(2):
                m.Comment.objects.create(article=article, body=f"Comment {a}-{j}-{k}")

        for b in range(3):
            book = m.Book.objects.create(
                title=f"Book {a}-{b}", publisher=publishers[(a + b) % 5]
            )
            book.authors.add(author)
            book.genres.set(genres[b % 4 : b % 4 + 2])
            for r in range(2):
                m.Review.objects.create(
                    book=book, reviewer=reviewers[(b + r) % 8], rating=r + 1
                )
