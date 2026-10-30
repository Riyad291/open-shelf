"""remove commerce; contribution replaces purchase

Revision ID: 7a64ad26d3b9
Revises: e14e4811fabc
Create Date: 2026-10-30 12:00:00

Written by hand. Autogenerate produced the right operations in the wrong
order: it dropped ``orders`` before ``order_items`` and ``payments``, which
still referenced it, and PostgreSQL refused.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7a64ad26d3b9'
down_revision: Union[str, Sequence[str], None] = 'e14e4811fabc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop the shop, and turn purchase-based standing into earned standing.

    Three things make this more than a list of drops:

    * **Order.** A table is dropped only after every table that references
      it: payments and order_items before orders, cart_items before carts.
    * **NOT NULL columns on existing rows.** ``trusted_reviewer`` and
      ``promotion_blocked`` are added with a server default so existing
      rows get a value, and the default is removed afterwards so the model
      stays the single source of defaults.
    * **SQLite.** It cannot drop a column in place, so the column changes run
      in batch mode, which rebuilds the table. A CHECK constraint naming a
      column has to be dropped in its own batch before the column, or the
      rebuilt table would still mention it.
    """
    op.drop_index('ix_payments_user_id', table_name='payments')
    op.drop_index('ix_payments_order_id', table_name='payments')
    op.drop_table('payments')

    op.drop_index('ix_order_items_order_id', table_name='order_items')
    op.drop_index('ix_order_items_book_id', table_name='order_items')
    op.drop_table('order_items')

    op.drop_index('ix_transactions_user_id', table_name='transactions')
    op.drop_index('ix_transactions_order_id', table_name='transactions')
    op.drop_index('ix_transactions_kind_created', table_name='transactions')
    op.drop_index('ix_transactions_kind', table_name='transactions')
    op.drop_index('ix_transactions_book_id', table_name='transactions')
    op.drop_table('transactions')

    op.drop_index('ix_orders_user_status', table_name='orders')
    op.drop_index('ix_orders_user_id', table_name='orders')
    op.drop_index('ix_orders_status', table_name='orders')
    op.drop_table('orders')

    op.drop_index('ix_cart_items_cart_id', table_name='cart_items')
    op.drop_index('ix_cart_items_book_id', table_name='cart_items')
    op.drop_table('cart_items')
    op.drop_table('carts')

    with op.batch_alter_table('books') as batch:
        batch.drop_constraint('ck_books_price_non_negative', type_='check')
        batch.drop_constraint('ck_books_stock_non_negative', type_='check')
    with op.batch_alter_table('books') as batch:
        batch.drop_column('stock')
        batch.drop_column('price')

    with op.batch_alter_table('reviews') as batch:
        batch.add_column(sa.Column('trusted_reviewer', sa.Boolean(),
                                   nullable=False,
                                   server_default=sa.false()))
        batch.drop_column('verified_purchase')
    with op.batch_alter_table('reviews') as batch:
        batch.alter_column('trusted_reviewer', server_default=None)

    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('promotion_blocked', sa.Boolean(),
                                   nullable=False,
                                   server_default=sa.false()))
        batch.drop_column('vip_ineligible')
        batch.drop_column('balance')
        batch.drop_column('total_fines')
    with op.batch_alter_table('users') as batch:
        batch.alter_column('promotion_blocked', server_default=None)


def downgrade() -> None:
    """Put the shop back, empty. The money that was in it is not recoverable."""
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('total_fines', sa.Numeric(precision=10, scale=2),
                                   nullable=False, server_default='0'))
        batch.add_column(sa.Column('balance', sa.Numeric(precision=10, scale=2),
                                   nullable=False, server_default='0'))
        batch.add_column(sa.Column('vip_ineligible', sa.Boolean(),
                                   nullable=False, server_default=sa.false()))
        batch.drop_column('promotion_blocked')
    with op.batch_alter_table('users') as batch:
        batch.alter_column('total_fines', server_default=None)
        batch.alter_column('balance', server_default=None)
        batch.alter_column('vip_ineligible', server_default=None)

    with op.batch_alter_table('reviews') as batch:
        batch.add_column(sa.Column('verified_purchase', sa.Boolean(),
                                   nullable=False, server_default=sa.false()))
        batch.drop_column('trusted_reviewer')
    with op.batch_alter_table('reviews') as batch:
        batch.alter_column('verified_purchase', server_default=None)

    with op.batch_alter_table('books') as batch:
        batch.add_column(sa.Column('price', sa.Numeric(precision=10, scale=2),
                                   nullable=False, server_default='0'))
        batch.add_column(sa.Column('stock', sa.Integer(), nullable=False,
                                   server_default='0'))
    with op.batch_alter_table('books') as batch:
        batch.alter_column('price', server_default=None)
        batch.alter_column('stock', server_default=None)
        batch.create_check_constraint('ck_books_price_non_negative',
                                      'price >= 0')
        batch.create_check_constraint('ck_books_stock_non_negative',
                                      'stock >= 0')

    # The tables exactly as the initial schema created them.
    op.create_table('carts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id')
    )
    op.create_table('orders',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('subtotal', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('discount', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('total', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('discount_rate', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('placed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status in ('pending','paid','cancelled','refunded')", name='ck_orders_status'),
    sa.CheckConstraint('subtotal >= 0 and total >= 0 and discount >= 0', name='ck_orders_amounts_non_negative'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_orders_status'), 'orders', ['status'], unique=False)
    op.create_index(op.f('ix_orders_user_id'), 'orders', ['user_id'], unique=False)
    op.create_index('ix_orders_user_status', 'orders', ['user_id', 'status'], unique=False)
    op.create_table('cart_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('cart_id', sa.Integer(), nullable=False),
    sa.Column('book_id', sa.Integer(), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('quantity > 0', name='ck_cart_item_quantity_positive'),
    sa.ForeignKeyConstraint(['book_id'], ['books.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['cart_id'], ['carts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('cart_id', 'book_id', name='uq_cart_item_book')
    )
    op.create_index(op.f('ix_cart_items_book_id'), 'cart_items', ['book_id'], unique=False)
    op.create_index(op.f('ix_cart_items_cart_id'), 'cart_items', ['cart_id'], unique=False)
    op.create_table('order_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=False),
    sa.Column('book_id', sa.Integer(), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('unit_price', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('line_total', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('title_at_purchase', sa.String(length=255), nullable=False),
    sa.CheckConstraint('quantity > 0', name='ck_order_item_quantity_positive'),
    sa.CheckConstraint('unit_price >= 0 and line_total >= 0', name='ck_order_item_amounts_non_negative'),
    sa.ForeignKeyConstraint(['book_id'], ['books.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_order_items_book_id'), 'order_items', ['book_id'], unique=False)
    op.create_index(op.f('ix_order_items_order_id'), 'order_items', ['order_id'], unique=False)
    op.create_table('payments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('method', sa.String(length=32), nullable=False),
    sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('reference', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status in ('captured','failed','refunded')", name='ck_payments_status'),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_payments_order_id'), 'payments', ['order_id'], unique=False)
    op.create_index(op.f('ix_payments_user_id'), 'payments', ['user_id'], unique=False)
    op.create_table('transactions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('book_id', sa.Integer(), nullable=True),
    sa.Column('order_id', sa.Integer(), nullable=True),
    sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('note', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind in ('purchase','fine','deposit','refund')", name='ck_transactions_kind'),
    sa.ForeignKeyConstraint(['book_id'], ['books.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_transactions_book_id'), 'transactions', ['book_id'], unique=False)
    op.create_index(op.f('ix_transactions_kind'), 'transactions', ['kind'], unique=False)
    op.create_index('ix_transactions_kind_created', 'transactions', ['kind', 'created_at'], unique=False)
    op.create_index(op.f('ix_transactions_order_id'), 'transactions', ['order_id'], unique=False)
    op.create_index(op.f('ix_transactions_user_id'), 'transactions', ['user_id'], unique=False)
