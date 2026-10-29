-- Orders owned by the orders service.
CREATE TABLE IF NOT EXISTS orders (
    id          uuid        PRIMARY KEY,
    user_id     text        NOT NULL,
    status      text        NOT NULL,
    total_cents bigint      NOT NULL DEFAULT 0,
    charge_id   text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_items (
    order_id         uuid    NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    sku              text    NOT NULL,
    qty              integer NOT NULL CHECK (qty > 0),
    unit_price_cents integer NOT NULL
);

-- This file runs on every start while other replicas serve traffic. CREATE
-- INDEX locks the table even when the index exists, so check the catalog first.
DO $$
BEGIN
    IF to_regclass('order_items_order_id_idx') IS NULL THEN
        CREATE INDEX order_items_order_id_idx ON order_items (order_id);
    END IF;
    IF to_regclass('orders_user_id_idx') IS NULL THEN
        CREATE INDEX orders_user_id_idx ON orders (user_id);
    END IF;
END $$;
