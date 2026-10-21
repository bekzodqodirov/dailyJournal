"""Telegram reader on the owner's personal account (spec §7B); its one write
path, ``sender``, sends only the outbox rows that passed the gate: the ones
the owner approved and, in auto mode, the ones Jarvis may send by itself
(JV-19)."""
