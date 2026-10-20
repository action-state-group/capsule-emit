# Threat model: recording agent actions

This is the general threat model for recording agent actions as Agent Action
Capsules. It applies to any producer: an agent framework, a gateway, a tool
call boundary, a ledger. Nothing here assumes a particular domain, transport or
model.

It covers what a record can and cannot establish, what each party to a recorded
action fears, which evidence addresses each fear, and one threat that is easy
to miss: the record is itself an attack surface against whoever reads it.

Two related documents:

- The record format is the Agent Action Capsule profile,
  `draft-mih-scitt-agent-action-capsule`, whose current posted revision is
  `-05` ([AAC-05]). On any conflict the profile text wins. Its Security
  Considerations section is the normative floor this document builds on.
- [`a2a-extension/threat-model.md`](a2a-extension/threat-model.md) is a
  narrower model for one composition, task evidence carried over an
  agent-to-agent protocol. It maps that proposal's threat list item by item to
  its reference implementation. This document does not repeat it.

Citations to code name the file and line at a fixed commit, so a claim can be
checked against the code that makes it true.

## 1. What a record can and cannot establish

A signed record does not make its producer trustworthy. It makes the
producer's claims durable, attributable to a key, and checkable after the fact.
The useful question is not "is this producer honest" but "if this goes wrong,
is there anything left to examine?"

A valid record establishes that the holder of a key signed these bytes, that
the bytes have not changed since, and, once registered, that they existed by a
point in time. It does not establish:

- **That the content is true.** A signature authenticates a statement; it does
  not make it true. A `confirmed` effect means the producer bound the bytes of a
  response it observed, not that the world reached the claimed state ([AAC-05]
  Security Considerations, [AAC-05-confirmed]).
- **Who the signer is.** The raw public key in a record is self-attested.
  Mapping a key to an operator, a developer or an authority is the relying
  party's policy, never a consequence of signature validity ([AAC-05-issuer],
  [AAC-05-signer]).
- **That this is the only or latest copy.** A producer can hold more than one
  history. Registration and witnessing narrow this (section 3, class A); a lone
  file cannot.

Three consequences run through the rest of this document:

1. **Accountability precedes assurance.** Most of the value arrives with a
   record that exists, is signed, and cannot be quietly revised. Further
   evidence narrows the space of undetectable lies; it never closes it.
2. **Self-attestation is the floor, not a flaw.** A producer signing a
   statement about its own behavior is as strong as its key's binding to an
   identity and as durable as the log it is registered in. Saying so plainly is
   what makes the record usable as evidence.
3. **Absence of evidence never reads as a pass.** A check that did not run, a
   value that was not supplied and a witness that was unreachable are distinct
   states, and none of them is "verified". A verifier reports the tier it
   actually checked and never upgrades a claim it could not check ([AAC-05]
   Security Considerations).

### 1.1 The client capability ladder

Most recorded actions start with a request from someone else: a user, another
agent, an orchestrator. Call that party the **client**. What a record can say
about the exchange depends on what the client contributed to it, and that
depends on what the client is already able to do.

| Rung | The client contributes | What the record then establishes | What it still does not establish |
|---|---|---|---|
| 0 | Nothing | A signed, chained, offline-checkable account by the producer, honest about later tampering | That any client ever made the request. A producer can author a plausible history from nothing |
| 1 | A fresh, unpredictable value bound into the record | The producer can no longer fabricate an exchange with this client, or replay an old one as fresh to it | **Any identity.** A fresh value is unpredictable and anonymous |
| 2 | A signature over a commitment to the exact request | Who asked (as far as the client's key is bound to an identity), that this record answers that exact request, and the authorization the client gave | That the client's key belongs to the person or organization it claims; that is the relying party's key policy, as for any signer |

**The step most often misread.** Rung 0 to rung 1 buys resistance to
fabrication and replay, not identity. Rung 1 to rung 2 buys who. These are two
steps, not one. A record that says a value was "supplied by the client" means
only that the producer had help; it identifies no one.

Two further points:

- **Rung 1 is evidence to the client, not to everyone.** Only the party that
  generated the value can tell that it was fresh. A later reader learns
  nothing from a random value unless the client vouches for it. The profile has
  no dedicated field for such a value; a producer that receives one commits it
  with the request it arrived in.
- **A transport binding is a weaker alternative to rung 1.** A value derived
  from the TLS session, through a keying material exporter ([RFC 5705]) or a
  TLS 1.3 channel binding ([RFC 9266]), costs the client nothing extra. It is
  per connection rather than per request and needs TLS end to end, so it is
  narrower than rung 1 and cannot reach rung 2.

**Why a ladder and not a required header.** The more capable a client's
identity already is, the higher the rung it reaches with less added work. A
client with no key must have a fresh-value mechanism built for it; a client
that already holds a key need only use it. Requiring every client to send the
same thing either excludes the clients that cannot or wastes the ones that
already can. A deployment states which rung each class of client reaches and
records that rung honestly.

**How the profile records rung 2.** When the client is a counterparty that
returns its own signed half, the record's `cross_party` block and
`assurance.cross_party_rung` (`unilateral_fallback`, `acknowledged_receipt`,
`full_bilateral`) say how much of that half is bound in. A verifier derives
the rung from the block's own bytes and reports an overclaim, but this is a
structural check: it does not verify the counterparty's signature itself,
which is checked where that half is verified ([AAC-05-crossparty]). A record
with no client contribution is `unilateral_fallback`.

## 2. Parties and what each fears

| Party | Holds | Wants |
|---|---|---|
| **Producer** | The runtime that takes the action and seals the record; a signing key | To act, and later to show what it did and why |
| **Client** | The request; possibly a key | That the action taken is the one asked for, and no more than it authorized |
| **Counterparty** | The system the action touched (a service, a tool, another agent) | Not to be blamed for an action it did not take, and to bind its own view of the exchange |
| **Relying party** | A decision that depends on the record now | To decide on evidence rather than on the producer's word |
| **Later reader** | Neither key nor context; arrives after the fact | To evaluate an action it did not witness: a reviewer, an auditor, a party to a dispute |
| **Log operator and witnesses** | A transparency log; checkpoint co-signing keys | To keep an append-only log that cannot show different views to different readers |

Every one of these parties also **reads** records, often through an assistant
model. Section 4 treats the reader as a target in its own right.

The later reader is why records must be portable and checkable offline.
Evidence that only works inside the producer's own tooling is not evidence to
anyone outside it.

### 2.1 What the client fears

| # | Fear | Evidence that addresses it | Limit |
|---|---|---|---|
| CL1 | The producer did something other than what was asked | Rung 2: a client signature over the request, bound into the record (section 1.1) | Below rung 2 the record is the producer's account alone |
| CL2 | An old result is replayed as fresh | Rung 1 or higher | A value the producer minted itself is not replay evidence (rule 2, section 5) |
| CL3 | The producer later denies or rewrites what it did | Signed, chained records; registration to a log the producer does not control | Chaining alone shows order within one history, not that it is the only history |
| CL4 | More was done than authorized | The authorization bound and what was performed recorded as two separate facts | A record shows both; it cannot stop an action, only show it |
| CL5 | The request content leaks | Payloads committed by digest, never in clear ([context hygiene](context-hygiene.md)) | Low-entropy values are recoverable by guessing; the producer's runtime sees plaintext |

### 2.2 What the producer fears

| # | Fear | Evidence that addresses it | Limit |
|---|---|---|---|
| P1 | An action it was asked to take is attributed to it alone | A client signature over the request (rung 2) | Attribution is to a key; binding that key to a person is policy |
| P2 | It is accused of an action it did not take, or of taking it differently | Its own signed, registered record; non-repudiation works for the signer too | Only for actions it recorded |
| P3 | A refusal reads as a failure | A record on every verdict, with the reason class ([AAC-05] Disposition) | None at the record layer |
| P4 | A spoofed response makes it record a success that did not happen | Upstream input-integrity evidence referenced by digest ([AAC-05-input]) | The profile does not authenticate upstream responses itself |
| P5 | Its records become a liability because they hold personal data | The three-class admission floor: identity and secrets never enter ([context hygiene](context-hygiene.md)) | A record is unerasable by design; protection must happen before the seal |

### 2.3 What the counterparty fears

| # | Fear | Evidence that addresses it | Limit |
|---|---|---|---|
| CP1 | The producer's record misstates what the counterparty did | The counterparty's own signed half, referenced from the record (`cross_party`) | The verifier of the record checks the reference's shape, not the half's signature ([AAC-05-crossparty]) |
| CP2 | A dispute becomes one word against another | Both halves bound to one exchange, registered where neither party controls the log | Needs both parties to sign |

### 2.4 What the relying party and the later reader fear

| # | Fear | Evidence that addresses it | Limit |
|---|---|---|---|
| R1 | The record cannot be checked without the producer's cooperation | Offline verification from the record and bundle alone | Witness receipts need the witnesses' keys (section 4.4) |
| R2 | The record is internally valid and describes a fiction | Key policy (who may sign for what), registration, counterparty halves | A compromised producer can sign true-shaped statements about false events. This is the permanent floor |
| R3 | A different history is shown to each reader | Registration, and witnesses co-signing the log's checkpoint | One log alone can still equivocate; witnesses reduce this to collusion |
| R4 | A check that never ran looks like a pass | Three states everywhere; verifiers list what they did not check | Enforced per implementation |
| R5 | The rules in force at the time cannot be established | Constraint and manifest references in the record, and configuration epochs ([AAC-05] Configuration epochs) | The manifest itself must be retained and disclosed |
| R6 | **Reading the record harms the reader** | Section 4 | Readable fields cannot be made inert |

### 2.5 Adversaries in scope

- **A dishonest producer:** fabricates or replays records, keeps more than one
  history, overstates its assurance, ships a verification page that lies.
- **A dishonest client:** asks for an action, then denies having asked;
  disputes a correct result.
- **A content injector:** anyone who can put text into a field the producer
  will record in clear, through a tool argument, an identifier, a response or
  a disclosed payload (section 4).
- **A passive observer** of registered records: learns sizes, timing and
  cadence, and can guess low-entropy committed values.

### 2.6 Not defended against

- **A producer reading the requests it serves.** Plaintext is in its memory by
  construction. Records discard bodies; runtimes do not.
- **Correctness of the action.** A faithful record of a wrong action is a
  correct record.
- **A compromised producer** signing well-formed statements about events that
  did not happen.
- **Collusion between a producer and the only log it registers in.**
- **Traffic analysis and timing side channels.**

## 3. Assurance by evidence class

The classes are separate, not rungs of one ladder: a record can be strongly
registered and carry no identity binding, or carry a counterparty half and
never be registered. Each class reports one of three states: absent, present
but not checked, or checked (and a failed check is a checked state).

| Class | Establishes | Does not establish | Where it lives |
|---|---|---|---|
| **A. Record integrity** | Recorded: a signed commitment to the action. Chained: order within one history. Registered: the record existed in a log by a point in time, shown by a Receipt. Witnessed: other parties co-signed the log's checkpoint | Registered alone: that the log shows every reader the same view. Witnessed: that the witnesses are honest, only that equivocation needs their collusion | [AAC-05-registration]; [`checkpoint.md`](checkpoint.md) |
| **B. Signer identity** | Which key signed | Whose key it is, or what it may sign for | [AAC-05-issuer] |
| **C. Client and counterparty** | The rung of section 1.1; the `cross_party_rung` | Truth of either party's statement | [AAC-05-crossparty] |
| **D. Effect** | That the producer bound the bytes of a response it observed | That the world reached the state, or that the responder was authentic | [AAC-05-confirmed] |
| **E. Confidentiality** | Payloads committed by digest, revealed only by disclosure | Secrecy of low-entropy values; secrecy from the producer's own runtime | [context hygiene](context-hygiene.md); [AAC-05] Privacy Considerations |

Three acts in class A are kept apart. **Registration** is submitting a signed
statement to a transparency service ([RFC 9943]). A **Receipt** is what the
service returns, a signed inclusion proof ([RFC 9942]). **Witnessing** is a
separate party co-signing the log's checkpoint so the log cannot present
different views to different readers. A good Receipt from an unwitnessed log
is registered, not witnessed.

## 4. The reader as a target

### 4.1 The threat

**A record is an attack surface against whoever reads it.** Increasingly that
reader is a language model: an operator triaging with an assistant, an auditor
asking a model to summarize a ledger, an agent reading another agent's
history before it acts.

Committing payloads by digest keeps request and response bodies out of the
record, and that is often described as making the record content-private
([anatomy](anatomy.md)). Content-private is not inert. A record must keep some
fields readable, or nobody can ask it what happened to a given order, account
or task. Fields that stay readable include:

- correlation handles and names: `action_id`, `operator`, `developer`, and
  identifiers a producer records so related records can be found;
- `effect` type and status, and the `disposition` verdict;
- any payload once it is disclosed for checking;
- producer-written notes carried in presentation extensions.

When a producer fills a readable field from input an attacker can influence,
such as a tool argument, an identifier passed in by a counterparty, or a
response it recorded, then text planted in that input reaches every later
reader intact, signed and registered. A signature makes such text more
convincing, not less: it is authentically what the producer recorded. A
planted instruction disguised as an identifier is the typical shape.

**Readability and injection exposure are the same property.** A field that
must stay readable for the record to answer questions is a vector, and
encrypting the record does not close it, because a field that must stay
queryable under encryption is readable to whoever queries it.

### 4.2 What does not change

- Payload bodies are still committed by digest and not retained by the record
  layer ([anatomy](anatomy.md)). This threat concerns readable metadata and
  disclosed values, not bodies.
- Every record legitimately carries plaintext metadata. The answer is not to
  digest everything; a record nobody can query answers no questions.
- The record's integrity guarantees are unaffected. A field carrying injected
  text verifies correctly, because it is exactly what was signed.

### 4.3 What limits it

At the producer:

- Admit context by allow-list, so only fields the adapter names can enter a
  record ([context hygiene](context-hygiene.md)). This bounds which fields can
  carry injected text; it does not clean them.
- Prefer opaque handles the producer minted over values copied from input.

At the page that renders a bundle, the presentation contract [AAC-PC] limits
what bundle content can do:

- A declarative module has no free-text kind and never displays prose taken
  from a payload. Its `identifier` kind shows a short token (no whitespace, at
  most 128 characters) verbatim, so a short planted token still reaches the
  reader ([AAC-PC-declarative]).
- Producer notes are shown only as text, never as markup, under a fixed label
  saying they are the producer's note and not evidence, and never at the first
  depth level ([AAC-PC-chrome]).
- Wording, depth, verdicts and check lists never come from the bundle
  ([AAC-PC-chrome], [AAC-PC-checklists]).
- A page built by the reference emitter carries a Content-Security-Policy
  that runs only scripts whose hashes it lists, and allows no network
  connection, form submission or base URL change ([emitter-csp]).

At the reader: treat every value read from a record as data to quote, never as
an instruction to follow, however authentic its signature. A verified record
says who wrote the bytes, not that the bytes are safe to act on.

### 4.4 The page verifier: what it checks and what it cannot

A bundle is often handed to a reader as a self-contained page that verifies
itself in the browser. Two limits matter.

**The page is delivered by the producer.** The verification code ships inside
the file it verifies. An honest builder pins that code by hash and sets the
Content-Security-Policy above, but a dishonest producer can ship a page whose
verifier says anything. A page's own verdict is therefore evidence only of
what an honest copy of that page would say. To rely on a bundle, check it with
a verifier you obtained separately. capsulectl's `verify --bundle` takes the
bundle file, and also reads the bundle embedded in a self-contained receipt
page ([capsulectl-receipt-page]). capsulectl writes a page only for a bundle
its own `verify --bundle` calls VALID ([capsulectl-page-gate]), but a reader
cannot tell from a page alone which tool built it.

**The in-page runtime runs some checks and not others.** The page lists, from
its own results and never from the bundle, what it checked, what failed and
what it did not check ([AAC-PC-checklists], [page-checks]). In a browser:

| Check | On the page | With capsulectl `verify --bundle` |
|---|---|---|
| Each record's contents against its capsule ID | Checked | Checked |
| Each record's required fields, effect, verdict, chain parent and assurance claims | Checked | Checked |
| Graph closure from the root record | Checked | Checked |
| Range completeness and per-record membership in the log | Checked when the bundle carries a completeness certificate and checkpoint | Checked |
| Disclosed values against their committed digests | Checked | Checked |
| Countersignatures over the bundle digest | Checked when the browser provides Ed25519; otherwise not checked ([page-countersign]) | Not checked by `verify --bundle`, which lists each as unverified ([go-countersign]); checked by `countersign verify`, which resolves signers against a directory the reader chooses ([capsulectl-countersign]) |
| Producer signature on the records a page module cites as signers | Checked, marked beside each signer | Checked (as part of every record) |
| **Producer signature on every record** | **Not checked** ([page-not-checked]) | Checked, under the key the record names ([capsulectl-signatures]) |
| **The log checkpoint's COSE signature** | **Not checked in a browser**: the authenticator is available only outside the browser, so the page reports the claims that rest on it as passing without it and says so ([page-checkpoint]) | Checked ([capsulectl-checkpoint]) |
| **Witness receipts** | **Not checked** ([page-not-checked]) | Checked only against a witness directory the reader supplies; with none, no receipt is checked ([capsulectl-witness]) |

Signatures over the whole record set are therefore a full-verifier check: a
page that says "passed" has not checked that every record was signed by the
key it names, nor that the checkpoint and its receipts are genuine. The two
verifiers are not nested, though: countersignatures are checked on the page and
by `countersign verify`, not by `verify --bundle`. A reader who needs every
check runs both commands.

**What neither verifier establishes.** The page says so in its own words:
none of its checks says who produced the records, or that the file is the most
recent copy ([page-scope], [AAC-PC-checklists]). The same holds for capsulectl:

- A producer signature is checked under the public key the record itself
  carries ([capsulectl-signatures]). That proves which key signed, not whose
  key it is. Key authorization stays with the relying party ([AAC-05-signer]).
- A checkpoint signature is likewise checked under the key the checkpoint
  names ([cll-checkpoint-key]). Whether that is the log you meant to trust is
  your decision, made by pinning its key.
- A witness receipt counts only under a key you listed for that witness.
  Without a directory you chose, no witness is trusted.
- Freshness: a valid bundle can be an old one. Only a checkpoint you obtained
  independently, or a witness you trust, shows whether there is a newer state.

## 5. Vocabulary rules

1. **Three states, never two.** Absent, present but not checked, checked. A
   failed check is worth more than silence and is never shown as a pass.
2. **No silent upgrade.** A fallback never takes the label of what it
   replaced. A value the producer generated is not client evidence; a
   structurally derived rung is not a verified signature.
3. **A claim is not evidence.** A stated model, a configured manifest or a
   recorded identifier is a claim. The record says which it holds.
4. **A signature names a key, not a person.** Say "signed by key K", and name
   the policy that maps K to a party.
5. **Content-private is not inert.** Never describe a record as safe to read
   because its payloads are digested. Readable fields are an injection surface
   (section 4).
6. **Checked here is not checked.** Say where a check ran. A page that did not
   check a signature never calls it verified.
7. **Registration, Receipt and witnessing are three things.** Use those words
   for those acts. This document avoids "anchor" as a verb: it is not the
   vocabulary of the relevant RFCs and collides with "trust anchor". The
   profile's field value `attestation_mode: "anchored"` is unchanged.
8. **RFC citations.** The SCITT architecture is [RFC 9943]; COSE Receipts are
   [RFC 9942]. RFC 9162 (Certificate Transparency 2.0) is named only when its
   `RFC9162_SHA256` Merkle tree algorithm specifically is meant.

## 6. What this is not

This is a threat model for the record and its verification. It is not a
security review of any deployment, and it describes no operated service. A
deployment adds its own parties, transports and limits, and should state them
in its own document, referencing this one for the general case.

[AAC-05]: https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/
[AAC-05-issuer]: https://github.com/action-state-group/agent-action-capsule/blob/572b00795dc627033422e2ce711173ca9a50bcfb/spec/draft-mih-scitt-agent-action-capsule-05.md?plain=1#L256-L264
[AAC-05-registration]: https://github.com/action-state-group/agent-action-capsule/blob/572b00795dc627033422e2ce711173ca9a50bcfb/spec/draft-mih-scitt-agent-action-capsule-05.md?plain=1#L266-L290
[AAC-05-crossparty]: https://github.com/action-state-group/agent-action-capsule/blob/572b00795dc627033422e2ce711173ca9a50bcfb/spec/draft-mih-scitt-agent-action-capsule-05.md?plain=1#L621-L638
[AAC-05-confirmed]: https://github.com/action-state-group/agent-action-capsule/blob/572b00795dc627033422e2ce711173ca9a50bcfb/spec/draft-mih-scitt-agent-action-capsule-05.md?plain=1#L1769-L1780
[AAC-05-input]: https://github.com/action-state-group/agent-action-capsule/blob/572b00795dc627033422e2ce711173ca9a50bcfb/spec/draft-mih-scitt-agent-action-capsule-05.md?plain=1#L1808-L1826
[AAC-05-signer]: https://github.com/action-state-group/agent-action-capsule/blob/572b00795dc627033422e2ce711173ca9a50bcfb/spec/draft-mih-scitt-agent-action-capsule-05.md?plain=1#L1841-L1845
[AAC-PC]: https://github.com/action-state-group/agent-action-capsule/blob/572b00795dc627033422e2ce711173ca9a50bcfb/spec/presentation-contract-v0.md
[AAC-PC-declarative]: https://github.com/action-state-group/agent-action-capsule/blob/572b00795dc627033422e2ce711173ca9a50bcfb/spec/presentation-contract-v0.md?plain=1#L444-L474
[AAC-PC-chrome]: https://github.com/action-state-group/agent-action-capsule/blob/572b00795dc627033422e2ce711173ca9a50bcfb/spec/presentation-contract-v0.md?plain=1#L476-L511
[AAC-PC-checklists]: https://github.com/action-state-group/agent-action-capsule/blob/572b00795dc627033422e2ce711173ca9a50bcfb/spec/presentation-contract-v0.md?plain=1#L527-L563
[page-checks]: https://github.com/action-state-group/agent-action-capsule/blob/572b00795dc627033422e2ce711173ca9a50bcfb/ts/src/verification-page.ts#L466-L591
[page-not-checked]: https://github.com/action-state-group/agent-action-capsule/blob/572b00795dc627033422e2ce711173ca9a50bcfb/ts/src/verification-page.ts#L556-L567
[page-scope]: https://github.com/action-state-group/agent-action-capsule/blob/572b00795dc627033422e2ce711173ca9a50bcfb/ts/src/verification-page.ts#L395-L399
[page-checkpoint]: https://github.com/action-state-group/agent-action-capsule/blob/572b00795dc627033422e2ce711173ca9a50bcfb/ts/src/bundle.ts#L951-L963
[page-countersign]: https://github.com/action-state-group/agent-action-capsule/blob/572b00795dc627033422e2ce711173ca9a50bcfb/ts/src/countersignature-stamp.ts#L222-L265
[capsulectl-checkpoint]: https://github.com/action-state-group/capsule-cli/blob/318d5f1f18deca0db619e82a71ff3352677341f5/internal/cli/verify_bundle.go#L24-L46
[capsulectl-signatures]: https://github.com/action-state-group/capsule-cli/blob/318d5f1f18deca0db619e82a71ff3352677341f5/internal/cli/verify_bundle.go#L94-L137
[capsulectl-witness]: https://github.com/action-state-group/capsule-cli/blob/318d5f1f18deca0db619e82a71ff3352677341f5/internal/cli/bundle_witness.go#L58-L97
[go-countersign]: https://github.com/action-state-group/agent-action-capsule/blob/572b00795dc627033422e2ce711173ca9a50bcfb/go/bundle/bundle.go#L179-L184
[capsulectl-countersign]: https://github.com/action-state-group/capsule-cli/blob/318d5f1f18deca0db619e82a71ff3352677341f5/internal/cli/countersign.go#L697
[capsulectl-receipt-page]: https://github.com/action-state-group/capsule-cli/blob/318d5f1f18deca0db619e82a71ff3352677341f5/internal/cli/verify_bundle.go#L379-L397
[capsulectl-page-gate]: https://github.com/action-state-group/capsule-cli/blob/318d5f1f18deca0db619e82a71ff3352677341f5/internal/cli/page_gate.go#L10-L40
[cll-checkpoint-key]: https://github.com/action-state-group/checkpointed-local-log/blob/40dabd74508d0f8649c8754873e316badef1057d/ts/src/checkpoint.ts#L427-L436
[emitter-csp]: https://github.com/action-state-group/capsule-viewer/blob/1d393519a0a2980e7baaa81e687f2a8bd68a18c6/ts/src/emitter.ts#L232-L248
[RFC 5705]: https://www.rfc-editor.org/rfc/rfc5705
[RFC 9266]: https://www.rfc-editor.org/rfc/rfc9266
[RFC 9942]: https://www.rfc-editor.org/rfc/rfc9942
[RFC 9943]: https://www.rfc-editor.org/rfc/rfc9943
