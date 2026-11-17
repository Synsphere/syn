"""Real PostgreSQL acceptance; uses Syn's existing disposable database fixture.

These tests are not substitutes for the SQLite replay/transport tests. The
normal verification workflow installs migrations before running -m database.
"""
import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.database


@pytest.mark.parametrize('operation', ['UPDATE', 'DELETE', 'TRUNCATE'])
def test_postgresql_witness_evidence_is_immutable(db, operation):
    digest = hashlib.sha256(uuid4().bytes).hexdigest()
    db.execute(text('''INSERT INTO "operatorDagWitnessRecords"
        (digest,"observerHash","peerHash",sequence,body,"receiptSequence","receiptHash")
        VALUES (:digest,:observer,:peer,1,'{}',1,:receipt)'''), {
            'digest': digest, 'observer': hashlib.sha256(uuid4().bytes).hexdigest(),
            'peer': hashlib.sha256(uuid4().bytes).hexdigest(), 'receipt': 'ab' * 32})
    statement = {
        'UPDATE': 'UPDATE "operatorDagWitnessRecords" SET body=\'{"changed":true}\' WHERE digest=:digest',
        'DELETE': 'DELETE FROM "operatorDagWitnessRecords" WHERE digest=:digest',
        'TRUNCATE': 'TRUNCATE TABLE "operatorDagWitnessRecords"',
    }[operation]
    savepoint = db.begin_nested()
    try:
        with pytest.raises(DBAPIError, match='immutable'):
            db.execute(text(statement), {'digest': digest})
    finally:
        savepoint.rollback()
    assert db.scalar(text('SELECT body FROM "operatorDagWitnessRecords" WHERE digest=:digest'),
                     {'digest': digest}) == '{}'
