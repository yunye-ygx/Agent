from app.core.config import Settings


def test_settings_expose_mysql_minio_rabbitmq_and_elasticsearch_defaults() -> None:
    config = Settings(_env_file=None)

    assert config.mysql_database == "rag_demo"
    assert config.mysql_url.startswith("mysql+asyncmy://")
    assert config.elasticsearch_url == "http://localhost:9200"
    assert config.elasticsearch_index == "rag_demo_chunks_v1"
    assert config.minio_files_bucket == "rag-demo-files"
    assert config.minio_artifacts_bucket == "rag-demo-artifacts"
    assert config.rabbitmq_exchange == "rag_demo.tasks"
    assert config.rabbitmq_queue == "rag_demo.task.execute"
    assert config.embedding_dimension == 2048
    assert config.run_background_services is True


def test_settings_build_urls_from_explicit_values() -> None:
    config = Settings(
        _env_file=None,
        mysql_host="db",
        mysql_port=3307,
        mysql_user="user",
        mysql_password="secret",
        mysql_database="demo",
        elasticsearch_url="http://es:9200",
        rabbitmq_url="amqp://guest:guest@mq:5672/",
    )

    assert config.mysql_url == "mysql+asyncmy://user:secret@db:3307/demo"
    assert config.elasticsearch_url == "http://es:9200"
    assert config.rabbitmq_url == "amqp://guest:guest@mq:5672/"
