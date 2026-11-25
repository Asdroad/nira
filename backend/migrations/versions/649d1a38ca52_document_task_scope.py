"""Allow the same uploaded offer in different tasks without moving the original."""
from alembic import op
import sqlalchemy as sa

revision = '649d1a38ca52'
down_revision = '30a12721f7c0'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('documents') as batch:
        batch.drop_constraint('uq_document_hash', type_='unique')
        batch.create_unique_constraint('uq_document_scope_hash', ['organization_id', 'campaign_id', 'sha256'])


def downgrade():
    duplicates = op.get_bind().execute(sa.text('SELECT organization_id, sha256 FROM documents GROUP BY organization_id, sha256 HAVING count(*) > 1')).first()
    if duplicates:
        raise RuntimeError('Cannot restore organization-wide deduplication while offers are used in multiple tasks; no documents were removed')
    with op.batch_alter_table('documents') as batch:
        batch.drop_constraint('uq_document_scope_hash', type_='unique')
        batch.create_unique_constraint('uq_document_hash', ['organization_id', 'sha256'])
