"""Always-on shopper traffic against the gateway."""

import os
import random
import uuid

from locust import HttpUser, between, task

HOT_SKUS = int(os.getenv("HOT_SKUS", "5000"))


def random_sku() -> str:
    return f"SKU-{random.randint(1, HOT_SKUS):07d}"


class Shopper(HttpUser):
    wait_time = between(0.5, 2.0)

    def on_start(self):
        self.user_id = f"user-{uuid.uuid4().hex[:8]}"
        self.order_ids: list[str] = []

    @task(5)
    def browse_catalog(self):
        offset = random.randint(0, 50) * 20
        self.client.get("/products", params={"limit": 20, "offset": offset}, name="/products")

    @task(4)
    def view_product(self):
        self.client.get(f"/products/{random_sku()}", name="/products/[sku]")

    @task(3)
    def checkout(self):
        items = [{"sku": random_sku(), "qty": random.randint(1, 3)} for _ in range(random.randint(1, 3))]
        with self.client.post(
            "/checkout", json={"user_id": self.user_id, "items": items}, catch_response=True
        ) as response:
            if response.status_code == 201:
                self.order_ids = (self.order_ids + [response.json()["order_id"]])[-20:]
                response.success()
            elif response.status_code == 409:
                response.success()  # out of stock is a valid business outcome
            else:
                response.failure(f"checkout returned {response.status_code}")

    @task(1)
    def view_order(self):
        if self.order_ids:
            self.client.get(f"/orders/{random.choice(self.order_ids)}", name="/orders/[id]")
