"""The act layer on top of MIYA (docs/jarvis-plan.md).

Everything under this package is type-checked with mypy in strict mode
(``make typecheck``) and held to the no-placeholder rule from its first
module on. Core MIYA never imports it at startup: the bot's ``/jarvis``
handler and the worker's ``jarvis_outbox`` job import it when they run, and
the job exists only with JARVIS_ENABLED (rule 20). Importing this package
imports nothing else.

The core (JV-11):

* ``chatkey`` — channel-agnostic chat keys (``tg:``, ``wa:``, ``mail:``).
* ``policy`` — modes, pauses and breakers (``jarvis_policy``), reply rules
  and hard exclusions.
* ``caps`` and ``costs`` — the daily caps and the cost cap, counted in SQL.
* ``gate`` — the pure decision every outbox row passes when it is claimed,
  and the red-line guard interface (fails closed until JV-13 registers one).
* ``outbox`` — the one outbound chokepoint: the only code that creates
  ``outbox_messages`` rows or changes their state.
* ``status`` — what ``/jarvis`` shows.

The red lines (JV-13), pure:

* ``redlines`` — the deterministic inbound signals and the binding output
  guard (RED LINE 1 and 2, rule 14); ``outbox`` registers it with ``gate``.
* ``stalls`` — the fixed stall templates the client gets while the owner
  decides.

The owner's style (JV-14):

* ``style`` — the sampler (the owner's own words only, never a personal
  chat), deterministic traits, scrubbed few-shot examples, per-person tone,
  the profile versions and the owner's stall overrides.
* ``style_prompt`` — the prose prompt and its checks, the deterministic
  fallback prose and the style block a reply drafter reads (pure).
"""
