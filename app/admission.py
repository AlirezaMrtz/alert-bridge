from __future__ import annotations

from dataclasses import dataclass


class DeliveryAdmission:
    """Non-waiting, all-or-nothing reservations owned by one ASGI event loop."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("Delivery capacity must be positive")
        self.capacity = capacity
        self.in_use = 0

    def try_reserve(self, count: int) -> Reservation | None:
        if count < 1:
            raise ValueError("Reservation size must be positive")
        # No await between checking and updating: other requests cannot interleave.
        if self.in_use + count > self.capacity:
            return None
        self.in_use += count
        return Reservation(self, count)


@dataclass
class Reservation:
    admission: DeliveryAdmission
    count: int
    released: bool = False

    def release(self) -> None:
        if not self.released:
            self.admission.in_use -= self.count
            self.released = True
