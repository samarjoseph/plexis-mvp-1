"""
Tests for CSV upload size limit enforcement.

Covers:
- Pure-logic boundary tests: below limit (accepted), at limit (accepted), above limit (rejected)
- Configurable upload limit driven by Config.MAX_UPLOAD_SIZE_MB
- Backend rejection of oversized CSV with HTTP 413
- Correct JSON structure: error_code: 'FILE_TOO_LARGE', limit_mb: 35, status: 413
- Professional free-tier messaging without stack traces or internal details
- XLSX files are NOT subject to the CSV-specific 35 MB check
- Config default and env override behavior
"""
import io
import pytest
from unittest.mock import patch, MagicMock

CSV_LIMIT_MB = 35
BYTES_PER_MB = 1024 * 1024


def _make_csv_bytes(size_mb: float) -> bytes:
    """Generate a syntactically valid CSV of approximately `size_mb` MB."""
    header = b"id,value,description\n"
    row = b"1,test,padding_data_padding_data_padding_data_padding_data\n"
    target = int(size_mb * BYTES_PER_MB)
    repetitions = max(1, (target - len(header)) // len(row))
    return header + row * repetitions


def _make_xlsx_bytes(size_mb: float) -> bytes:
    """Return fake XLSX bytes of approximately `size_mb` MB (magic bytes + padding)."""
    xlsx_magic = b'PK\x03\x04'  # ZIP/XLSX magic
    padding = b'\x00' * max(0, int(size_mb * BYTES_PER_MB) - len(xlsx_magic))
    return xlsx_magic + padding


# ---------------------------------------------------------------------------
# Unit tests — direct size-check logic & boundaries
# ---------------------------------------------------------------------------

class TestUploadSizeCheckLogic:
    """Pure-logic boundary tests for the file-size enforcement in datasets.py."""

    def _check_csv_size(self, raw_bytes: bytes, limit_mb: int = CSV_LIMIT_MB):
        """Replicate the exact size-check logic from upload_dataset()."""
        file_size_mb = len(raw_bytes) / BYTES_PER_MB
        return file_size_mb > limit_mb, round(file_size_mb, 1)

    def test_csv_below_limit_accepted(self):
        data = _make_csv_bytes(10)  # 10 MB — well below 35 MB
        rejected, size = self._check_csv_size(data)
        assert not rejected, f"10 MB CSV should not be rejected (size={size} MB)"

    def test_csv_at_limit_accepted(self):
        data = _make_csv_bytes(35)  # exactly 35 MB
        rejected, size = self._check_csv_size(data)
        assert not rejected, f"35 MB CSV should be accepted (size={size} MB)"

    def test_csv_above_limit_rejected(self):
        data = _make_csv_bytes(36)  # 36 MB — 1 MB over limit
        rejected, size = self._check_csv_size(data)
        assert rejected, f"36 MB CSV should be rejected (size={size} MB)"

    def test_csv_far_above_limit_rejected(self):
        data = _make_csv_bytes(100)  # 100 MB — far over limit
        rejected, size = self._check_csv_size(data)
        assert rejected, f"100 MB CSV should be rejected (size={size} MB)"

    def test_size_check_uses_configured_limit(self):
        """Size check respects a different configured limit."""
        data = _make_csv_bytes(20)
        rejected, _ = self._check_csv_size(data, limit_mb=15)  # lower limit
        assert rejected, "20 MB file should be rejected when limit is 15 MB"
        rejected2, _ = self._check_csv_size(data, limit_mb=25)  # higher limit
        assert not rejected2, "20 MB file should be accepted when limit is 25 MB"


# ---------------------------------------------------------------------------
# Integration tests — Flask test client
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    """Create a minimal Flask app with the datasets blueprint for testing."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from app import create_app as _create_app
    _app = _create_app()
    _app.config['TESTING'] = True
    _app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200 MB ceiling for tests
    return _app


@pytest.fixture
def client(app):
    return app.test_client()


class TestUploadEndpointSizeRejection:

    def _post_file(self, client, filename: str, data: bytes, content_type: str = 'text/csv'):
        return client.post(
            '/api/upload',
            data={'file': (io.BytesIO(data), filename)},
            content_type='multipart/form-data',
            headers={'X-User-Email': 'test@plexis.local'},
        )

    def test_oversized_csv_returns_413(self, client):
        """Backend must reject oversized CSV with HTTP 413."""
        # Test with limit=1 MB and a 2 MB file for fast execution
        data = _make_csv_bytes(2)
        with patch('api.datasets.Config') as mock_cfg:
            mock_cfg.MAX_UPLOAD_SIZE_MB = 1
            mock_cfg.DATABASE_URL = None
            response = self._post_file(client, 'oversized.csv', data)
        assert response.status_code == 413, f"Expected 413, got {response.status_code}"

    def test_413_response_structure_and_error_code(self, client):
        """413 response must be JSON and include error_code: FILE_TOO_LARGE."""
        data = _make_csv_bytes(2)
        with patch('api.datasets.Config') as mock_cfg:
            mock_cfg.MAX_UPLOAD_SIZE_MB = 1
            mock_cfg.DATABASE_URL = None
            response = self._post_file(client, 'oversized.csv', data)
        assert response.status_code == 413
        body = response.get_json()
        assert body is not None, "413 response must be valid JSON"
        assert body.get('error_code') == 'FILE_TOO_LARGE'
        assert body.get('limit_mb') == 1
        assert body.get('status') == 413

    def test_413_response_professional_messaging(self, client):
        """413 response user_message must contain the required free-tier messaging without stack traces."""
        data = _make_csv_bytes(2)
        with patch('api.datasets.Config') as mock_cfg:
            mock_cfg.MAX_UPLOAD_SIZE_MB = 35
            mock_cfg.DATABASE_URL = None
            # file is 36 MB when limit is 35 MB
            data_36 = _make_csv_bytes(36)
            response = self._post_file(client, 'oversized.csv', data_36)
        assert response.status_code == 413
        body = response.get_json()
        msg = body.get('user_message', '')

        # Check required phrasing elements per specification
        assert "Need to upload a larger dataset?" in msg
        assert "free development phase" in msg
        assert "CSV uploads limited to 35 MB" in msg
        assert "upcoming paid plans" in msg
        assert "apologize for the inconvenience" in msg.lower()
        assert "patience" in msg.lower()

        # Ensure no internal error or stack traces leaked
        assert "traceback" not in msg.lower()
        assert "exception" not in msg.lower()
        assert "error 500" not in msg.lower()

    def test_xlsx_not_subject_to_csv_limit(self, client):
        """XLSX files are not rejected by the CSV-specific limit."""
        data = _make_xlsx_bytes(2)
        with patch('api.datasets.Config') as mock_cfg, \
             patch('api.datasets.dataset_storage') as mock_storage, \
             patch('api.datasets.dataset_loader') as mock_loader, \
             patch('api.datasets.dataset_profiler') as mock_profiler, \
             patch('api.datasets.dataset_intelligence_engine') as mock_engine, \
             patch('api.datasets._try_persist_dataset_to_db', return_value=None):
            mock_cfg.MAX_UPLOAD_SIZE_MB = 1  # 1 MB CSV limit, but file is XLSX
            mock_cfg.DATABASE_URL = None
            mock_storage.save.return_value = '/tmp/test.xlsx'
            import pandas as pd
            mock_loader.load.return_value = pd.DataFrame({'id': [1], 'name': ['test']})
            mock_profiler.profile.return_value = {}
            mock_dko = MagicMock()
            mock_dko.columns = {}
            mock_dko.get_dataset_identity.return_value = MagicMock(probable_purpose='test')
            mock_dko.fingerprint = 'fp_xlsx'
            mock_engine.analyze.return_value = mock_dko
            mock_engine.get_frontend_payload.return_value = {}

            response = self._post_file(client, 'spreadsheet.xlsx', data)
            # Must NOT be 413
            assert response.status_code != 413, "XLSX should not trigger CSV 413 rejection"


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

class TestUploadSizeConfig:

    def test_default_limit_is_35_mb(self, monkeypatch):
        """Config.MAX_UPLOAD_SIZE_MB default must be 35 MB when env var is absent."""
        monkeypatch.delenv('MAX_UPLOAD_SIZE_MB', raising=False)
        import importlib, config as cfg_module
        importlib.reload(cfg_module)
        assert cfg_module.Config.MAX_UPLOAD_SIZE_MB == 35, \
            f"Default MAX_UPLOAD_SIZE_MB should be 35 MB, got {cfg_module.Config.MAX_UPLOAD_SIZE_MB}"

    def test_limit_override_via_env(self, monkeypatch):
        """MAX_UPLOAD_SIZE_MB can be overridden via environment variable."""
        monkeypatch.setenv('MAX_UPLOAD_SIZE_MB', '50')
        import importlib, config as cfg_module
        importlib.reload(cfg_module)
        assert cfg_module.Config.MAX_UPLOAD_SIZE_MB == 50
        # Reset back
        monkeypatch.setenv('MAX_UPLOAD_SIZE_MB', '35')
        importlib.reload(cfg_module)
        assert cfg_module.Config.MAX_UPLOAD_SIZE_MB == 35
