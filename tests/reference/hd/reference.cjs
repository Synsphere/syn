// Test-only independent Rust/WASM implementation; no runtime dependency.
// Every key below comes from fixed, public test entropy. Print public data only.
const C = require('@emurgo/cardano-serialization-lib-nodejs');
const crypto = require('node:crypto');
const hard = (i) => i + 0x80000000;
const raw = (key) => Buffer.from(key.to_public().to_raw_key().as_bytes());
const hash = (key) => crypto.createHash('sha256').update(raw(key)).digest('hex');
const master = C.Bip32PrivateKey.from_bip39_entropy(Buffer.from([...Array(32).keys()]), Buffer.alloc(0));
const root = master.derive(hard(6969)).derive(hard(420));
const base = root.derive(hard(0)).derive(0).derive(0);
const message = Buffer.from('Syn independent Icarus operator reference v1');
const candidate_hashes = {};
for (const inheritance of [0, 172941, 16777215]) {
  const parent = root.derive(hard(inheritance));
  candidate_hashes[String(inheritance)] = [...Array(8).keys()].map(i => hash(parent.derive(i + 1).derive(0)));
}
process.stdout.write(JSON.stringify({
  profile: 'syn.hd-independent-reference/1',
  root_public: raw(root).toString('hex'),
  base_public: raw(base).toString('hex'),
  root_hash: hash(root),
  base_hash: hash(base),
  signature: Buffer.from(base.to_raw_key().sign(message).to_bytes()).toString('hex'),
  candidate_hashes,
}) + '\n');
