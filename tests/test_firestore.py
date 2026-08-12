"""FirestoreLedger tests with a stubbed google.cloud.firestore client.

We monkeypatch nine.ledger.firestore_ledger's firestore import so no
credentials/emulator are needed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from nine.ledger.firestore_ledger import FirestoreLedger


class FakeDoc:
    def __init__(self, data):
        self._data = data
        self.exists = bool(data)

    def get(self):
        return self

    def to_dict(self):
        return self._data

    def set(self, data, merge=False):
        self._data = {**self._data, **data} if merge else data
        self.exists = True

    def update(self, data):
        self._data.update(data)

    def delete(self):
        self._data = {}


class FakeStream:
    def __init__(self, docs):
        self._docs = docs

    def __iter__(self):
        return iter(self._docs)


class FakeCollection:
    def __init__(self):
        self.docs = {}

    def document(self, doc_id):
        if doc_id not in self.docs:
            self.docs[doc_id] = FakeDoc({})
        return self.docs[doc_id]

    def stream(self):
        return FakeStream(list(self.docs.values()))

    def limit(self, n):
        return self


class FakeFirestore:
    def __init__(self):
        self.collections = {}

    def collection(self, name):
        if name not in self.collections:
            self.collections[name] = FakeCollection()
        return self.collections[name]


@pytest.fixture
def fake_firestore(monkeypatch):
    fake = FakeFirestore()
    # firestore_ledger does `from google.cloud import firestore; firestore.Client()`
    # lazily — patch the real module's Client so no creds/emulator are needed.
    import google.cloud.firestore as fs

    monkeypatch.setattr(fs, "Client", lambda *a, **kw: fake)
    return fake


def test_firestore_ledger_submit_and_fetch(fake_firestore):
    led = FirestoreLedger(collection="nine-jobs")
    job = led.submit("research", {"task": "x"})
    got = led.get(job.job_id)
    assert got.job_id == job.job_id
    assert got.workflow_id == "research"
    assert got.status == "submitted"


def test_firestore_ledger_update_and_stats(fake_firestore):
    led = FirestoreLedger(collection="nine-jobs")
    job = led.submit("build", {"task": "y"})
    job.transition("routing")
    job.transition("running")
    job.transition("awaiting_evidence")
    job.transition("shipped")
    led.update(job)
    stats = led.stats()
    assert stats["total"] == 1
    assert stats["by_status"]["shipped"] == 1


def test_firestore_ledger_discover(fake_firestore):
    led = FirestoreLedger(collection="nine-jobs")
    led.submit("research", {"task": "a"})
    led.submit("research", {"task": "b"})
    jobs = led.discover()
    assert len(jobs) == 2


def test_firestore_ledger_artifacts_and_cancel(fake_firestore):
    led = FirestoreLedger(collection="nine-jobs")
    job = led.submit("build", {"task": "y"})
    job.artifacts = [{"name": "EVAL.json", "size": 3, "sha256": "abc", "produced_by": "build"}]
    led.update(job)
    arts = led.artifacts(job.job_id)
    assert arts[0]["name"] == "EVAL.json"
    led.cancel(job.job_id)
    assert led.status(job.job_id) == "cancelled"
