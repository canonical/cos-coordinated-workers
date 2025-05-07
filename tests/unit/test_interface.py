from cosl.interfaces.utils import DatabagModel


def test_databag_dump_update():
    class MyModel(DatabagModel):
        foo: int
        bar: str = "barian"

    db = {}
    assert MyModel(foo=1, bar="soz").dump(db)
    assert db == {"foo": "1", "bar": '"soz"'}


def test_databag_dumps():
    class MyModel(DatabagModel):
        foo: int
        bar: str = "barian"

    assert MyModel(foo=1, bar="bearian").dump() == {"foo": "1", "bar": '"bearian"'}
