import pytest
from django.test import Client


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker, tmp_path_factory):
    """Keep the test sqlite database out of the real /data volume."""
    yield


@pytest.fixture(scope="session")
def django_db_modify_db_settings(tmp_path_factory):
    from django.conf import settings
    data_dir = tmp_path_factory.mktemp("data")
    settings.DATABASES["default"]["NAME"] = str(data_dir / "db.sqlite3")


@pytest.fixture(autouse=True)
def _static_root(settings, tmp_path):
    settings.STATIC_ROOT = str(tmp_path / "static")


@pytest.fixture
def client():
    return Client()
