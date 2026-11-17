"""Complete HK Map Service's missing intermediate without adding a trust anchor.

On 2026-11-17 open.hkmapservice.gov.hk sent its leaf and root but omitted
Hongkong Post e-Cert SSL CA 3 - 17. This public intermediate was downloaded
with verified HTTPS from https://www1.ecert.gov.hk/root/ecert_ssl_ca_3-17.crt.
Its DER fingerprint matches Hongkong Post's 2025 WebTrust CA report:
https://www.ecert.gov.hk/product/cps/ecert/img/HKPCA_WebTrust_CA_2025.pdf

This is the 2025 reissue (serial 3DB180EF6FF85F7A895D16445B9FC9FC3DEFBBC7),
valid through 2032-06-03, not the revoked 2017 certificate with the same name.
The default system root store, hostname verification and certificate validity
checks remain mandatory. Do not use partial-chain trust or disable TLS checks.
"""
import hashlib
from pathlib import Path
import ssl


CERTIFICATE = Path(__file__).with_name("certs") / "hongkong-post-ssl-ca-3-17-2025.pem"
DER_SHA256 = "cb730c3a5fe958b8bdc5b363892c9f530b33f98a43ce3c2578fce096b4ae233c"


def verified_context():
    context = ssl.create_default_context()
    # Python versions may differ in their default flags. The intermediate is
    # chain material only: trust must terminate at an existing system root.
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    pem = CERTIFICATE.read_text(encoding="ascii")
    der = ssl.PEM_cert_to_DER_cert(pem)
    if hashlib.sha256(der).hexdigest() != DER_SHA256:
        raise ValueError("Unexpected HK Map Service intermediate certificate")
    # Load only the fingerprint-checked DER certificate. Appended PEM material
    # must never add another CA to this context.
    context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(der))
    return context
