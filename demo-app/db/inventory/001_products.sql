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

-- Every reservation and product lookup filters by sku.
CREATE UNIQUE INDEX IF NOT EXISTS products_sku_idx ON products (sku);
