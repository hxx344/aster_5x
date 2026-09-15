"""One owner for each account's transient scheduling state."""
from dataclasses import dataclass, field


@dataclass
class OrdinaryRead:
    account: dict
    symbols: tuple
    broker: object
    future: object
    priority: bool
    signals: dict


@dataclass
class CycleWake:
    seen: tuple | None = None
    opportunity: tuple | None = None
    signal: dict | None = None
    after: float = 0
    deferred: bool = False

    def clear_hint(self):
        self.signal = self.opportunity = None

    def rearm(self):
        self.seen = self.opportunity = None
        self.deferred = False


@dataclass
class AccountWork:
    wake: bool = False
    urgent: bool = False
    priority: dict = field(default_factory=dict)
    priority_levels: dict = field(default_factory=dict)
    followup: bool = False
    active_priority: bool = False
    active_signals: dict = field(default_factory=dict)
    backoff: float = 0
    quote_backoff: float | None = None
    cycle: CycleWake = field(default_factory=CycleWake)
    hot_wake: bool = False
    hot_backoff: float = 0
    ordinary_read: OrdinaryRead | None = None

    def take_priority(self):
        signals, self.priority = self.priority, {}
        return signals

    def take_active_signals(self):
        signals, self.active_signals = self.active_signals, {}
        return signals

    def start_priority(self):
        self.active_signals = self.take_priority()
        self.followup = False
        self.active_priority = True
