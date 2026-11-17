import pytest

def test_author_id_is_open_gap():
    from src.papertrail import author_id
    with pytest.raises(NotImplementedError):
        author_id.process({"id": "x"})

def test_version_is_open_gap():
    from src.papertrail import version
    with pytest.raises(NotImplementedError):
        version.process({"id": "x"})
