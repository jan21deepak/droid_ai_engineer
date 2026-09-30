# Reference

Lookup pages for the values and structures the code depends on. Each page is organized for scanning rather than reading end to end.

## Pages

- [Configuration](configuration.md) lists every environment variable, grouped by purpose, with defaults and whether it is required.
- [Data models](data-models.md) documents `TaskStatus` and each SQLAlchemy model, plus the `to_dict` shapes the API returns.
- [Dependencies](dependencies.md) lists the pinned runtime and test packages and the two external services the app talks to.

## Related pages

- [Configuration](../reference/configuration.md) is referenced from every setup path.
- [Persistence](../systems/persistence.md) covers how these models are stored and migrated.
- [REST endpoints](../api/rest-endpoints.md) for how `to_dict` shapes reach clients.
- [Getting started](../overview/getting-started.md) for the setup sequence that uses these values.
