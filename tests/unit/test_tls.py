import ssl

from hyperhttp.connection.tls import create_ssl_context


def test_verify_true_builds_default_context():
    ctx = create_ssl_context(verify=True)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_verify_false_disables_checking():
    ctx = create_ssl_context(verify=False)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_verify_with_ca_path(tmp_path):
    import certifi

    # Just reuse certifi's bundle at a custom path — create_ssl_context only
    # cares about the ``cafile=`` argument.
    ca_path = certifi.where()
    ctx = create_ssl_context(verify=ca_path)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_alpn_is_set():
    ctx = create_ssl_context(alpn_protocols=("http/1.1",))
    # set_alpn_protocols has no getter; set should succeed without raising.
    assert isinstance(ctx, ssl.SSLContext)


def test_op_no_compression_set():
    ctx = create_ssl_context()
    assert ctx.options & ssl.OP_NO_COMPRESSION


def test_cert_chain_single_file(tmp_path):
    import trustme

    ca = trustme.CA()
    cert = ca.issue_cert("localhost")
    combined = tmp_path / "combined.pem"
    with open(combined, "wb") as f:
        for pem in cert.cert_chain_pems:
            f.write(pem.bytes())
        f.write(cert.private_key_pem.bytes())

    ctx = create_ssl_context(verify=False, cert=str(combined))
    assert isinstance(ctx, ssl.SSLContext)


def test_cert_chain_two_tuple(tmp_path):
    import trustme

    ca = trustme.CA()
    cert = ca.issue_cert("localhost")
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    with open(cert_file, "wb") as f:
        for pem in cert.cert_chain_pems:
            f.write(pem.bytes())
    with open(key_file, "wb") as f:
        f.write(cert.private_key_pem.bytes())

    ctx = create_ssl_context(verify=False, cert=(str(cert_file), str(key_file)))
    assert isinstance(ctx, ssl.SSLContext)
