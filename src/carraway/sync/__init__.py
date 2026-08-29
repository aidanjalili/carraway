"""Optional bank synchronisation.

Carraway cannot offer free automatic bank sync, and neither can any other open
source project: aggregation providers charge per connected account per month,
and a project with no revenue cannot absorb that. So the model here is
bring-your-own-provider — the user holds an account with the provider, pays
them directly, and Carraway uses the credential they supply.

Nothing in this package is imported by the core, and the app works entirely
without it. File import stays the free path that always works.
"""

from .base import Provider, SyncResult

__all__ = ["Provider", "SyncResult"]
