"""Canonical management-command name for importing organizer product metadata."""

from .register_product import ProductImportCommand


class Command(ProductImportCommand):
    pass
