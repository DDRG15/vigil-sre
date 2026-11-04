"""Credential-like URL components are refused before persistence or logging."""
import pytest
import targetstore


@pytest.mark.parametrize('suffix', [
    '/health;token=FAKE_VALUE', '/a;token=FAKE_VALUE/health',
    '/health#access_token=FAKE_VALUE', '/health?jwt=FAKE_VALUE',
    '/health?session=FAKE_VALUE', '/health?bearer=FAKE_VALUE',
    '/health?client_jwt=FAKE_VALUE', '/health?jwt-token=FAKE_VALUE',
    '/health?session_id=FAKE_VALUE', '/health?session-id=FAKE_VALUE',
    '/health?client-bearer=FAKE_VALUE', '/health?bearer_value=FAKE_VALUE',
    '/health;%74oken=FAKE_VALUE', '/health#%74oken=FAKE_VALUE',
])
def test_credentials_rejected_in_all_components(suffix):
    url='https://example.test'+suffix
    with pytest.raises(targetstore.ValidationError,match='credential'):
        targetstore.validate_entry({'url':url})
    assert 'FAKE_VALUE' not in targetstore.redact_url(url)
    assert 'example.test' in targetstore.redact_url(url)


def test_public_key_name_remains_conservatively_rejected():
    with pytest.raises(targetstore.ValidationError,match='credential'):
        targetstore.validate_entry({'url':'https://example.test/?key=public-page'})


def test_clean_components_are_accepted():
    url='https://example.test/health;version=2?page=1#overview'
    assert targetstore.validate_entry({'url':url})['url']==url
