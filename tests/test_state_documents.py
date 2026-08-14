"""Document rows: upsert semantics, scoped deletes, orphan summary."""

from state import StateStore

from support import make_document


def test_get_missing_returns_none(state_store: StateStore) -> None:
    assert state_store.get_document("job-a", "local://job-a/nope.md") is None


def test_upsert_and_get_roundtrip(state_store: StateStore) -> None:
    doc = make_document(text_sha="t" * 64, media_type="text/markdown", chunk_count=4)
    state_store.upsert_document(doc)
    loaded = state_store.get_document(doc.job_id, doc.source)
    assert loaded == doc


def test_upsert_replaces_on_same_key(state_store: StateStore) -> None:
    state_store.upsert_document(make_document(chunk_count=4))
    state_store.upsert_document(make_document(chunk_count=9, status="failed_embed"))
    loaded = state_store.get_document("job-a", "local://job-a/doc.md")
    assert loaded is not None
    assert loaded.chunk_count == 9
    assert loaded.status == "failed_embed"
    assert state_store.count_documents("job-a") == 1


def test_same_rel_path_under_two_jobs(state_store: StateStore) -> None:
    state_store.upsert_document(make_document())
    state_store.upsert_document(
        make_document(job_id="job-b", source="local://job-b/doc.md")
    )
    assert state_store.count_documents("job-a") == 1
    assert state_store.count_documents("job-b") == 1


def test_delete_document(state_store: StateStore) -> None:
    state_store.upsert_document(make_document())
    state_store.delete_document("job-a", "local://job-a/doc.md")
    assert state_store.get_document("job-a", "local://job-a/doc.md") is None


def test_delete_documents_for_job(state_store: StateStore) -> None:
    for i in range(3):
        state_store.upsert_document(make_document(source=f"local://job-a/{i}.md"))
    state_store.upsert_document(make_document(job_id="job-b", source="local://job-b/x.md"))
    assert state_store.delete_documents_for_job("job-a") == 3
    assert state_store.count_documents("job-a") == 0
    assert state_store.count_documents("job-b") == 1


def test_delete_documents_for_collection(state_store: StateStore) -> None:
    state_store.upsert_document(make_document())
    state_store.upsert_document(
        make_document(job_id="job-b", source="local://job-b/x.md", collection="col-a")
    )
    state_store.upsert_document(
        make_document(job_id="job-c", source="local://job-c/y.md", collection="col-c")
    )
    assert state_store.delete_documents_for_collection("col-a") == 2
    assert state_store.count_documents("job-c") == 1


def test_list_sources(state_store: StateStore) -> None:
    for name in ("a.md", "b.md"):
        state_store.upsert_document(make_document(source=f"local://job-a/{name}"))
    assert state_store.list_sources("job-a") == {
        "local://job-a/a.md",
        "local://job-a/b.md",
    }


def test_list_documents_ordered(state_store: StateStore) -> None:
    for name in ("b.md", "a.md"):
        state_store.upsert_document(make_document(source=f"local://job-a/{name}"))
    sources = [doc.source for doc in state_store.list_documents("job-a")]
    assert sources == ["local://job-a/a.md", "local://job-a/b.md"]


def test_orphan_summary(state_store: StateStore) -> None:
    state_store.upsert_document(make_document())
    state_store.upsert_document(
        make_document(job_id="gone-job", source="local://gone/x.md", collection="col-z")
    )
    orphans = state_store.orphan_summary(known_job_ids={"job-a"})
    assert orphans == [{"job_id": "gone-job", "collection": "col-z", "state_rows": 1}]
