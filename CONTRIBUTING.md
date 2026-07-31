# Contributing

KCC721 is a draft protocol. Useful contributions include independent covenant
review, negative test cases, interoperable transaction builders, permissionless
indexers, wallet integration tests, and concrete specification corrections.

Please open an issue before making a large protocol change. A proposal should
state the invariant it changes, explain backward-compatibility and reorg impact,
and include an executable test vector where possible.

Pull requests should pass:

```bash
cd protocol/kcc721/engine && cargo test
cd ../../../reference && python3 -m unittest discover -s tests -v
```

Never include wallet seeds, private keys, access tokens, runtime databases, or
private blind-mint reveal artifacts in an issue or commit.
