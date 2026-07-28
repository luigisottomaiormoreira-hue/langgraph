"""Camada de integração: um adaptador por sistema externo.

Cada módulo expõe um `Protocol` (contrato), um cliente real e um `build_*`
que escolhe entre cliente real e simulado conforme credenciais e modo dry-run.
"""

from .hotmart import HotmartClient, build_hotmart_client
from .mailbox import MailboxClient, build_mailbox_client
from .meta_ads import MetaAdsClient, build_meta_client

__all__ = [
    "HotmartClient",
    "MailboxClient",
    "MetaAdsClient",
    "build_hotmart_client",
    "build_mailbox_client",
    "build_meta_client",
]
