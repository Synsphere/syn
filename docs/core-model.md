# Syn core model and application contracts

Reconciled on 1 November 2026. These contracts preserve the final choices from the
database, ontology and scheduler design conversations. The [architecture index](architecture.md)
separates them from later distributed-policy and deployment requirements.

## Vocabulary

Type is the adopted name for class; Thing is the adopted referent term. Ember
records known information about a subject. Pondle represents an objective
evaluation; this is its intended role, not proof that an assessment is correct.
Snug represents investment and emotional attachment. Cog represents something
capable of computation: a configured function, script, calculation or other
computational entity, including while idle. Its invocation is intended to be
triggered by an Event and to produce output of any Syn data type. The configured
Cog and each invocation have distinct identities. That Event-to-Cog execution
semantics is not automatically supplied by the existing Job scheduler.
Om is the karma unit associated with doing good deeds, not an implemented
currency, mining reward or universal score.

The original question definition is preserved exactly: **"A proposition which
can be answered, either yes or no, or with any other kind of response."** The
native ontology currently parents Enigma under question, but the historical
owner-supplied meaning recorded in the July account is a unit of trust. That
divergence remains unresolved; do not infer that the ancestry superseded the
intended meaning. Pondle's evaluation and Snug's attachment mappings are narrower
than their recorded reasoning/emotion roles. Reconcile explicitly before changing
root ancestry or reclassifying existing objects. Credential adds owner-held
identity material, with an optional unique public-key hash and private data that
must never be included in public payloads. [Ontology definitions](../src/syn/ontology.py)
record the implementation-era ancestry; historical naming candidates remain history.
The [cross-project semantic clarification](https://github.com/kieransimkin/potbot/blob/37aca16d7edb164ef39c1f3322f6033153330e39/docs/syn-primitive-semantics-and-ember-contracts.md)
records these distinctions and the richer Agent/Type contracts below.

An operational Agent is an autonomously acting entity or coordinated group under
one wallet hierarchy. Several cooperating workers can share that Agent identity.
The ontology's broad real-world `agent` class also includes people and
organizations; its ancestry alone does not supply a wallet or activate a software
Agent. Operational activation requires explicit identity/security bindings.

## Identity and integrity

Primitive records have one identity column, `id`, holding the URL-string
ObjectRef such as `syn://thing/example`. The kind and slug are encoded in it;
there is no duplicate slug column or UUID replacement for this local identity.
General ObjectRefs admit external and relative URL references. Local typed
references require the exact `syn://{kind}/{slug}` family. Only local references
participate in local registry validation/materialization; an external URL must
not cause a local placeholder.

Registry lifecycle, immutable identity, referential checks and truncate
protections belong in PostgreSQL as well as application contracts. Linking a
missing valid local object silently creates its placeholder, including the
slug-derived name and appropriate root classification. This is deliberate
incomplete knowledge, not a signal to discard the reference. Placeholders are
not trusted canonical Type releases or policy-adoption evidence.

Primitive additions retain the same reference-domain, registry, relationship,
chronicle and index functionality as the original families. Slugs have a
255-character domain bound; confidence, when expressed, lies between zero and
one; target positions are nonnegative; chronicle end times cannot precede their
beginnings and object/kind sequence numbers are unique. Integrity lives in the
schema, not only in a browser form. See the [constraint verification cases](../tests/test_database_integration.py).

The established database/object naming convention is camelCase. PostgreSQL
identifiers containing capital letters require quotes; no setting makes quoted
identifiers case-insensitive. Shared intelligence fields include knowledge,
statistics, confidence and their update times; unknown confidence is not zero.
Event targets, relationships and object chronicles are normalized tables rather
than embedded foreign-reference arrays. The chronicle separates ledger and
provenance histories. Existing portable protocol profiles retain their own frozen
field names and bytes; this naming convention does not silently rewrite them.

See [models](../src/syn/models.py), [ObjectRef](../src/syn/refs.py) and
[baseline schema](../database/objectReferenceSchema.sql). UUIDs used for jobs,
workspaces or policy catalogs do not replace primitive ObjectRefs.

## Classification

Each non-Type primitive has exactly one required direct `typeRef` on its own
table. Every primitive has a root Type. The old `objectTypes` link table is
removed; the current inherited-object view is `objectTypeAncestry`.

A Type may have multiple parents: the subtype graph is a DAG, not a single-parent
tree. Subtype edges must remain acyclic. Generic relationships have their own
meanings and are not forced into that subtype DAG. Parent updates reject missing
Types, self-parenting, duplicates, cycles and a selected parent that is already
an ancestor of another selected parent at any depth. Keep only the most specific
independent parent branches.

When an imported Thing has several independent classifications, reuse a matching
intersection Type or create a validated one. That single direct Type supplies
the inherited classifications. Legacy unclassified records fall back to the
appropriate primitive root. Type properties declare a name, expected Type and
minimum/maximum cardinality; they make expected structure explicit without
proving every arbitrary knowledge dictionary conforms to it.

## Inherited and instance Ember contracts

The cross-project clarification requires Types to supply versioned default
Embers as well as declarations of required/optional instance Embers. A binding
can inherit a shared algorithm or implementation Ember without making a mutable
copy. Slots distinguish the Ember's Type from the represented value/referent
Type, cardinality, satisfaction source, disclosure, override/combination rule
and revision provenance. Native `TypeProperty.propertyTypeRef` currently describes
the value/referent Type; it must not be silently repurposed as an Ember Type.

Pin the complete parent DAG and contract revision. Deduplicate the same inherited
Ember reached through a diamond; conflicting incomparable defaults require an
explicit compatible resolution. Children and overrides cannot weaken inherited
requirements or disclosure restrictions. Fixed bindings cannot be replaced;
required own-instance bindings cannot be satisfied by another Agent's default.
Missing, inaccessible/redacted and explicitly null values remain distinct.
Redaction must not trigger fallback to an inherited signing handle.

Membership, conformance and activation eligibility are separate results. A public
projection can omit private required bindings without declaring the underlying
owner-visible object incomplete or leaking the existence of its private slots.
Positive allowlists and access checks apply to exports and embedding inputs;
private keys, recovery material and private Ember payloads never become public
defaults, artifacts or vectors.

The security example links Agent -> Credential -> configured locking Cog ->
version-pinned algorithm/implementation Embers, with instance-local identity,
policy and protected operation handles. Publish only the approved public
verification material: a public-key hash identifies a key, while signature
verification also needs its actual public key. Public verification does not
authorize execution. Preserve distinct base/hourly artifact-signing and assigned
Agent account/group profiles; neither silently replaces the other.

Potbot documents an implemented resolver and custody integration for this richer
contract. The native Syn model's generic JSON, property rows and Cog ancestry
alone do not implement all of it. Upstream ownership and migration remain subject
to the [official-release boundary](development/release-and-node-deployment.md),
without silently rewriting frozen catalogs or claiming native-host acceptance.

## Type interfaces

| Surface | Contract |
| --- | --- |
| `GET /api/type/{id}` | Returns `syn://type/{id}` and recursively nested parent branches. |
| `GET /api/type?query=...` | Searches name and ID; deterministic relevance-ranked maximum of ten results. |
| `PUT /api/type/{id}` | Updates an existing Type; URL identity remains authoritative. Omitted parents preserve them; supplied parents receive full validation. |
| `GET /api/type/{id}/properties` | Lists declared properties. |
| `POST /api/type/{id}/properties` | Adds a property with unique `(type, name)`, Type reference and cardinalities. |
| `PUT /api/type/{id}/property/{name}` | Edits the named property. The earlier ambiguous request is resolved by this implemented update contract; deletion has its own method. |
| `DELETE /api/type/{id}/property/{name}` | Removes the property. |
| `GET /api/type/{id}/tree?depth=10` | Descendant tree; default depth ten, accepted depth zero through one hundred. |
| `/editTypes` | AJAX Type search, editable record, parent paths/editor directly after ObjectRef, property list and create/edit dialogs with Type autocomplete. |
| `/exploreTypes` | D3 collapsible descendant explorer using the API and separate primitive roots, including Credential. |

Property bounds are `min >= 0` and `max >= min`, with both defaulting to one.
The original D3 example attribution remains in the interface source. These UI
contracts are source-verified here; no fresh browser rendering was claimed.
See the [controller](../src/syn/controllers/type.py) and
[templates](../src/syn/templates).

## Persistence and configuration

Syn is a uv-managed Python application using SQLAlchemy 2 and PostgreSQL.
Production connection details are loaded from the selected ignored `config.toml`
through `loadConfig()`, never copied into source, examples, reports or persisted
job arguments. `SYN_CONFIG` selects another configuration file. Explicit
disposable test-service URLs and runtime-bound worker parameters are separate
scoped contracts; they are not permission to hard-code a live database URL.

Normal `createEngine()` startup applies Alembic migrations unless explicitly
disabled for separately managed deployment. The baseline includes domains,
functions and triggers; autogeneration alone cannot author those changes.
Complete legacy schemas can be validated/stamped; partial unversioned schemas
must be refused. Large seed imports remain explicit operations, outside automatic
startup migration. Database-writing jobs are not part of this documentation audit.

## Data import and observation

Wikipedia referents are Things, with their titles, source references and
descriptions retained. Imports are re-runnable, with collision handling and
provenance. Feed imports accept many source formats, including query-dependent
APIs; RSS is not a universal requirement. Normalize URLs safely, merge duplicate
master rows, retain all relevant source columns in camelCase knowledge fields,
record descriptions and provenance, and use companion research for ontology
classification rather than importing duplicate rows. A recorded source URL is
not a permanent guarantee that the source is active or usable.

`witness-stale-feeds` takes up to 100 feeds using their oldest stored parsed-entry
updates, with unknown updates first. This is the implemented selection rule,
not a claim that it sorts solely by last request time. It ranks candidates by
the count of updates in the seven days ending at the newest entry divided by
the lag to the previous request, bounded below at one hour. It processes the
highest scores until fifty candidates remain by default; failed attempts also
consume a candidate. A short source set can produce fewer than fifty attempts.

Each successfully recorded encounter has an observation Event, an
`observation-result` Ember and a separate `network-measurement` Ember. Preserve
the complete response bytes within the configured size bound, including a
Base64 representation, request/response headers, source URL, answering IP,
parsed timestamps and links between all three records. The earlier use of
Things for the two results was corrected; it must not return in future examples.

Follow 301/302 redirects up to the explicit hop limit, repeating public-address
safety checks at every destination. Record DNS, TCP, TLS, HTTP, transfer and
connection-close timings, per-hop logs and detailed failures. Capture the peer
certificate chain, certificate types and public-key material. Use the packaged
checksum-pinned trusted CA bundle with LF bytes. Sensitive header values are
redacted before constructing persisted objects; that is not a universal body
privacy guarantee. Dry-run remains a separate no-persistence mode.

See [feed import](../src/syn/feeds.py), [witnessing](../src/syn/feed_jobs.py),
[database seed guide](../database/README.md) and [HTTP verification cases](../tests/test_feed_http.py).

## Execution and networking

AsyncProcessScheduler runs explicitly registered process-safe handlers. Jobs
retain arguments, status, results, errors and times in PostgreSQL. Scheduler
startup requeues queued/interrupted-running jobs; consumers must not infer
exactly-once external effects from that recovery. Cancellation and result/status
queries have explicit API surfaces. The conventional Job layer does not prove
distributed deterministic Cog execution.

Primitive placeholder controllers use singular `/api/{primitive}` routes.
`uv run syn-server` starts Uvicorn and the Jinja-rendered welcome page, loads
configuration and handlers, and applies enabled migrations. Type controllers
are database-backed; placeholder routes must remain labelled as placeholders.

libp2p is managed with application startup/shutdown. The established defaults
listen on wildcard IPv4 and IPv6 interfaces using ephemeral TCP ports, with
configurable bootstrap/listen addresses, built-in mDNS LAN discovery and peer
status. The separate direct zeroconf implementation was abandoned in favour of
libp2p's discovery integration. Optional UPnP maps supported TCP listeners and
removes mappings on graceful shutdown; discovery and mapping success do not
prove connectivity, account authentication or private application exchange.

See [scheduler](../src/syn/scheduler.py), [API](../src/syn/api.py),
[peer service](../src/syn/p2p.py), [configuration](../config.example.toml),
[HTTP communication](communication.md) and the [target mesh](architecture.md).
Diagrams are in the architecture/overview guides; a screenshot or video would
not establish these database and execution guarantees.
