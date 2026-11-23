"""Delete every Neo4j Entity with extraction_method='manual'.

DESTRUCTIVE: manual entities are user-curated data, across every
organization. The default run only counts them; --yes deletes them. Exits
non-zero on any error.
"""

import argparse
import logging
import os
import sys
from typing import Optional

from neo4j import GraphDatabase

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

COUNT_QUERY = """
MATCH (e:Entity)
WHERE e.extraction_method = 'manual'
RETURN count(e) AS manual_count
"""

DELETE_QUERY = """
MATCH (e:Entity)
WHERE e.extraction_method = 'manual'
DETACH DELETE e
RETURN count(e) AS deleted_count
"""


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes", action="store_true", help="Actually delete. Default is a count only."
    )
    return parser.parse_args(argv)


def cleanup_manual_entities(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD")
    if not password:
        logger.error("NEO4J_PASSWORD environment variable is required")
        return 2

    logger.info(f"Target Neo4j: {uri}")
    driver = None
    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        with driver.session() as session:
            count = session.run(COUNT_QUERY).single()["manual_count"]
            if not args.yes:
                logger.info(
                    f"DRY RUN: {count} entities with extraction_method='manual' "
                    "would be deleted. Re-run with --yes to delete them."
                )
                return 0
            deleted = session.run(DELETE_QUERY).single()["deleted_count"]
            logger.info(f"Deleted {deleted} entities with extraction_method='manual'")
            return 0
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
        return 1
    finally:
        if driver is not None:
            driver.close()


if __name__ == "__main__":
    sys.exit(cleanup_manual_entities())
