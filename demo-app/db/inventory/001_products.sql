-- Product catalog owned by the inventory service.
CREATE TABLE IF NOT EXISTS products (
    id               bigserial   PRIMARY KEY,
    sku              text        NOT NULL,
    name             text        NOT NULL,
    description      text        NOT NULL,
    unit_price_cents integer     NOT NULL CHECK (unit_price_cents > 0),
    stock            integer     NOT NULL CHECK (stock >= 0),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Every reservation and product lookup filters by sku. Runs on every start, so
-- only take the table lock when the index is actually missing.
DO $$
BEGIN
    IF to_regclass('products_sku_idx') IS NULL THEN
        CREATE UNIQUE INDEX products_sku_idx ON products (sku);
    END IF;
END $$;
