from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
from .models import load_dotenv


@dataclass
class Settings:
    data_dir: Path
    admin_token: str
    read_token: str

    @classmethod
    def from_env(cls):
        load_dotenv()
        root = Path(os.environ.get('RAG_DATA_DIR', 'data/rag_work')).resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = root / 'access_tokens.json'
        saved = json.loads(path.read_text()) if path.exists() else {}
        admin = os.environ.get('RAG_ADMIN_TOKEN') or saved.get('admin') or secrets.token_urlsafe(32)
        read = os.environ.get('RAG_READ_TOKEN') or saved.get('read') or secrets.token_urlsafe(32)
        if len(admin) < 12 or len(read) < 12 or admin == read:
            raise ValueError('管理令牌和只读令牌须不同，且分别至少12位')
        if not path.exists():
            with path.open('x') as f:
                json.dump({'admin': admin, 'read': read}, f)
            path.chmod(0o600)
        return cls(root, admin, read)
