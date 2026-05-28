from django.db import models


class Widget(models.Model):
    """Trivial model for the DB-bound benchmark scenario."""

    name = models.CharField(max_length=100)
    value = models.IntegerField(default=0)
