"""Offline regression checks for the narrowly pinned missing-intermediate repair."""
import binascii
import hashlib
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest import mock

import hkmap_tls


class HKMapTLSTests(unittest.TestCase):
    def setUp(self):
        self.pem = hkmap_tls.CERTIFICATE.read_text(encoding="ascii")
        self.der = ssl.PEM_cert_to_DER_cert(self.pem)

    def test_current_certificate_fingerprint(self):
        self.assertEqual(
            hashlib.sha256(self.der).hexdigest(),
            "cb730c3a5fe958b8bdc5b363892c9f530b33f98a43ce3c2578fce096b4ae233c",
        )
        self.assertEqual(self.pem.count("-----BEGIN CERTIFICATE-----"), 1)

    def test_real_context_retains_required_verification_and_system_roots(self):
        baseline = ssl.create_default_context()
        actual = hkmap_tls.verified_context()
        self.assertEqual(actual.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(actual.check_hostname)
        partial = getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
        self.assertEqual(actual.verify_flags, baseline.verify_flags & ~partial)
        self.assertGreater(len(baseline.get_ca_certs(binary_form=True)), 0)
        self.assertTrue(
            set(baseline.get_ca_certs(binary_form=True)).issubset(
                set(actual.get_ca_certs(binary_form=True))
            )
        )

    def test_partial_chain_default_is_explicitly_cleared(self):
        partial = 0x80000
        unrelated = 0x8000 | 0x20
        context = mock.Mock()
        context.verify_flags = unrelated | partial
        with mock.patch.object(ssl, "VERIFY_X509_PARTIAL_CHAIN", partial, create=True), \
             mock.patch.object(ssl, "create_default_context", return_value=context):
            self.assertIs(hkmap_tls.verified_context(), context)
        self.assertEqual(context.verify_flags, unrelated)
        context.load_verify_locations.assert_called_once_with(
            cadata=ssl.DER_cert_to_PEM_cert(self.der)
        )

    def test_older_runtime_without_partial_chain_flag(self):
        context = mock.Mock()
        context.verify_flags = 0x8000
        with mock.patch.object(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0, create=True), \
             mock.patch.object(ssl, "create_default_context", return_value=context):
            hkmap_tls.verified_context()
        self.assertEqual(context.verify_flags, 0x8000)

    def test_wrong_certificate_is_rejected_before_loading(self):
        # A different DER fingerprint remains invalid even if its PEM parses.
        changed = self.der[:-1] + bytes([self.der[-1] ^ 1])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong.pem"
            path.write_text(ssl.DER_cert_to_PEM_cert(changed), encoding="ascii")
            context = mock.Mock(verify_flags=0x8000)
            with mock.patch.object(hkmap_tls, "CERTIFICATE", path), \
                 mock.patch.object(ssl, "create_default_context", return_value=context):
                with self.assertRaisesRegex(ValueError, "Unexpected HK Map Service"):
                    hkmap_tls.verified_context()
            context.load_verify_locations.assert_not_called()

    def test_appended_certificate_cannot_be_loaded_as_another_ca(self):
        changed = self.der[:-1] + bytes([self.der[-1] ^ 1])
        appended = self.pem + ssl.DER_cert_to_PEM_cert(changed)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "appended.pem"
            path.write_text(appended, encoding="ascii")
            context = mock.Mock(verify_flags=0x8000)
            with mock.patch.object(hkmap_tls, "CERTIFICATE", path), \
                 mock.patch.object(ssl, "create_default_context", return_value=context):
                try:
                    hkmap_tls.verified_context()
                except (ValueError, binascii.Error):
                    # PEM parsers may reject a concatenated input outright.
                    context.load_verify_locations.assert_not_called()
                else:
                    context.load_verify_locations.assert_called_once_with(
                        cadata=ssl.DER_cert_to_PEM_cert(self.der)
                    )

    def test_missing_certificate_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            context = mock.Mock(verify_flags=0x8000)
            with mock.patch.object(hkmap_tls, "CERTIFICATE", Path(directory) / "missing.pem"), \
                 mock.patch.object(ssl, "create_default_context", return_value=context):
                with self.assertRaises(FileNotFoundError):
                    hkmap_tls.verified_context()
            context.load_verify_locations.assert_not_called()

    def test_malformed_certificate_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "malformed.pem"
            path.write_text("not a certificate", encoding="ascii")
            context = mock.Mock(verify_flags=0x8000)
            with mock.patch.object(hkmap_tls, "CERTIFICATE", path), \
                 mock.patch.object(ssl, "create_default_context", return_value=context):
                with self.assertRaises(ValueError):
                    hkmap_tls.verified_context()
            context.load_verify_locations.assert_not_called()


if __name__ == "__main__":
    unittest.main()
