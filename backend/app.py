"""Flask application factory."""
import logging
import sys
from flask import Flask, jsonify
from flask_cors import CORS
from config import Config
from core.middleware import init_middleware, setup_logging
from core.errors import register_error_handlers
from api.health import health_bp
from api.datasets import datasets_bp
from api.datasets_data import datasets_data_bp
from api.chat import chat_bp
from api.spreadsheet_ops import spreadsheet_bp
from api.analysis import analysis_bp
from api.early_access import early_access_bp


def _verify_db_migration(app: Flask) -> None:
    """
    Verify the database is at the expected Alembic migration revision.

    If DATABASE_URL is not configured, skip silently (dev without DB).
    If DB is behind, log a clear error and exit(1).
    Do NOT auto-run migrations — that is a manual deployment step:
        python -m alembic -c alembic.ini upgrade head
    """
    from config import Config
    if not Config.DATABASE_URL:
        app.logger.warning(
            "[DB] DATABASE_URL not set — PostgreSQL persistence disabled. "
            "Set DATABASE_URL in .env for multi-user support."
        )
        return

    try:
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
        from alembic.config import Config as AlembicConfig
        from db.engine import engine
        import os

        backend_dir = os.path.dirname(os.path.abspath(__file__))
        alembic_cfg = AlembicConfig(os.path.join(backend_dir, "alembic.ini"))
        alembic_cfg.set_main_option("script_location", os.path.join(backend_dir, "alembic"))
        script = ScriptDirectory.from_config(alembic_cfg)
        head_revision = script.get_current_head()

        with engine.connect() as conn:
            context = MigrationContext.configure(conn)
            current_rev = context.get_current_revision()

        if current_rev != head_revision:
            auto_migrate = os.environ.get("AUTO_MIGRATE_DB", "true").lower() in ("true", "1", "yes")
            if auto_migrate:
                app.logger.warning(
                    "[DB] Schema revision mismatch (current: %s, head: %s). Running auto-migration to head...",
                    current_rev, head_revision
                )
                from alembic import command
                command.upgrade(alembic_cfg, "head")
                app.logger.info("[DB] Successfully auto-migrated schema to head revision %s", head_revision)
            else:
                app.logger.error(
                    "[DB] Schema out of date! Current: %s, Head: %s. "
                    "Run: python -m alembic -c alembic.ini upgrade head",
                    current_rev, head_revision,
                )
                sys.exit(1)
        else:
            app.logger.info("[DB] Schema up to date at revision: %s", current_rev)

    except SystemExit:
        raise
    except Exception as e:
        app.logger.error("[DB] Migration check failed: %s — continuing anyway", e)


def create_app() -> Flask:
    """Create and configure the Flask application."""
    Config.validate()

    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = (Config.MAX_UPLOAD_SIZE_MB + 5) * 1024 * 1024

    CORS(app, origins=Config.CORS_ORIGINS, supports_credentials=True)

    setup_logging(Config.LOG_LEVEL)
    init_middleware(app)
    register_error_handlers(app)

    # Initialize PostgreSQL DB session management
    if Config.DATABASE_URL:
        from db.session import init_db
        init_db(app)
        _verify_db_migration(app)

    # Initialize LLM providers and multi-slot credential pools
    from providers.engine import provider_engine

    app.register_blueprint(health_bp)
    app.register_blueprint(datasets_bp)
    app.register_blueprint(datasets_data_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(spreadsheet_bp)
    app.register_blueprint(analysis_bp)
    app.register_blueprint(early_access_bp)

    # Register transformation API blueprint
    try:
        from api.transform import transform_bp
        app.register_blueprint(transform_bp)
    except ImportError as e:
        logger.warning("[app] transform_bp not loaded: %s", e)

    # Register chats API blueprint
    try:
        from api.chats import chats_bp
        app.register_blueprint(chats_bp)
    except ImportError:
        pass  # Not yet implemented — will be added in Phase 6

    @app.route('/health', methods=['GET'])
    def root_health():
        return jsonify({
            "status": "healthy",
            "service": "plexis-backend",
            "version": "2.0.0"
        })

    logger = logging.getLogger()
    logger.info("Plexis backend initialized")

    return app
