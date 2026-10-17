"""Engine options that the long-lived worker children depend on."""

from repolace_shared.db.session import create_engine


def test_the_engine_pings_a_pooled_connection_before_using_it():
    engine = create_engine("postgresql+asyncpg://u:p@localhost/db")
    assert engine.sync_engine.pool._pre_ping is True
