"""The act layer on top of MIYA (docs/jarvis-plan.md).

Everything under this package is type-checked with mypy in strict mode
(``make typecheck``) and held to the no-placeholder rule from its first
module on. Core MIYA never imports it at startup: the bot's ``/jarvis``
handler and the worker's ``jarvis_outbox`` job import it when they run, and
the job exists only with JARVIS_ENABLED (rule 20); the userbot imports the
responder only inside ``enqueue_reply``, and ``chat_state`` only inside its
owner-first signal handlers, after the JARVIS_ENABLED check.
Importing this package imports nothing else.

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

The knowledge base (JV-15):

* ``kb`` — the owner-approved facts and answers: the writer that refuses
  red lines and a client's personal data, hybrid retrieval, the active
  answers the outbox reads for the statement allowlist, and the weekly
  suggestions mined from the owner's own answers (never used before the
  owner's tap).

The responder (JV-16):

* ``responder`` — a client's private message to a proposal: the enqueue the
  userbot calls after ``ingest_message``, the debounce and owner-first
  window, the deterministic signals first, then intent, KB, draft and the
  guards; every outcome an outbox row or an escalation with a stall.
* ``envelope`` — the client's thread as quoted, cleaned, untrusted data.
* ``intent`` — what the message asks (deterministic greeting, else
  EXTRACT_MODEL strict JSON).
* ``draft`` — the reply writer (REASON_MODEL, no tools, no private data).
* ``guard`` — the model guard (EXTRACT_MODEL): it can only block, and reads
  the thread for spam, scams and hijacked accounts.
* ``costs.ask`` is the one way these calls reach a model: nothing over the
  cost cap, usage recorded as ``jarvis_intent``, ``jarvis_draft`` and
  ``jarvis_guard``.

The owner first (JV-17):

* ``chat_state`` — the owner's signals (read, typing, their own message)
  and the client's (an edit, a deletion) turned into chat state and outbox
  changes: the owner's own message cancels the chat's drafts, closes its
  escalation and pauses the AI there for JARVIS_TAKEOVER_MINUTES.

The cards (JV-18):

* ``cards`` — what the owner sees and taps: draft cards and escalation
  alerts (the latter at once, in the responder's run), their buttons under a
  row lock, the owner's own words by reply, the snooze as a DB row, the
  daily alert fold and the copy mode until a channel can send. The worker's
  ``jarvis_cards`` job and the bot's ``jv:``/``je:`` handlers import it when
  they run.
"""
