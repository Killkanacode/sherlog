-- Seed one million products on first boot only.
INSERT INTO products (sku, name, description, unit_price_cents, stock)
SELECT
    'SKU-' || lpad(g::text, 7, '0'),
    (ARRAY['Classic', 'Smart', 'Eco', 'Pro', 'Mini', 'Ultra', 'Vintage', 'Travel'])[1 + g % 8]
        || ' '
        || (ARRAY['Mug', 'Lamp', 'Backpack', 'Notebook', 'Headphones', 'Bottle', 'Chair', 'Keyboard', 'Jacket', 'Watch'])[1 + (g / 8) % 10]
        || ' #' || g,
    repeat(md5(g::text), 3),
    199 + (g::bigint * 7919) % 49800,
    1000000
FROM generate_series(1, 1000000) AS g
WHERE NOT EXISTS (SELECT 1 FROM products LIMIT 1);

ANALYZE products;
