#!/bin/bash
# =============================================================================
# Backup and Restore Script for Knowledge Graph Analytics Dashboard
# =============================================================================
# This script provides automated backup and restore functionality for all
# critical components of the application including databases, file storage,
# and Kubernetes configurations.

set -euo pipefail

# Backups hold database contents and Helm values: keep every local file
# owner-only. Kubernetes Secrets are never backed up here, neither in the
# config dump nor in the Velero backup; after a restore, re-create them from
# the secret manager.
umask 077

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# The live app namespace on AWS EKS (nous-dev-cluster). The RDS checks look
# here, so a wrong default would make them miss.
NAMESPACE="${NAMESPACE:-multimodal-rag-system}"
# Velero's CLI --namespace is where Velero itself runs, not the app namespace.
VELERO_NAMESPACE="${VELERO_NAMESPACE:-velero}"
BACKUP_DIR="${BACKUP_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/kg-backups.XXXXXX")}"
mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
S3_BUCKET="${S3_BUCKET:-knowledge-graph-analytics-backups}"
# Optional customer-managed key; without it S3 uses the aws/s3 KMS key.
BACKUP_KMS_KEY_ID="${BACKUP_KMS_KEY_ID:-}"
LOG_FILE="$BACKUP_DIR/backup_${TIMESTAMP}.log"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1" | tee -a "$LOG_FILE"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1" | tee -a "$LOG_FILE"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1" | tee -a "$LOG_FILE"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1" | tee -a "$LOG_FILE"
}

# Upload one file to S3 with server-side KMS encryption.
s3_upload() {
    local src="$1" key="$2"
    local kms_args=()
    if [ -n "$BACKUP_KMS_KEY_ID" ]; then
        kms_args=(--sse-kms-key-id "$BACKUP_KMS_KEY_ID")
    fi
    aws s3 cp "$src" "s3://$S3_BUCKET/$key" --sse aws:kms "${kms_args[@]+"${kms_args[@]}"}"
}

# Check dependencies
check_dependencies() {
    log_info "Checking dependencies..."

    local missing_deps=()

    if ! command -v kubectl &> /dev/null; then
        missing_deps+=("kubectl")
    fi

    if ! command -v aws &> /dev/null; then
        missing_deps+=("aws")
    fi

    if ! command -v helm &> /dev/null; then
        missing_deps+=("helm")
    fi

    if ! command -v velero &> /dev/null; then
        missing_deps+=("velero")
    fi

    if [ ${#missing_deps[@]} -ne 0 ]; then
        log_error "Missing dependencies: ${missing_deps[*]}"
        exit 1
    fi

    log_success "All dependencies are available"
}

# Create backup directory
create_backup_dir() {
    log_info "Creating backup directory: $BACKUP_DIR"
    mkdir -p "$BACKUP_DIR"
    log_success "Backup directory created"
}

# On AWS the database is RDS: the chart creates the ExternalSecret
# aws-database-credentials (templates/aws-rds-secret.yaml) in the app
# namespace. Check the ExternalSecret, never the Secret's data.
uses_rds() {
    kubectl get externalsecret aws-database-credentials -n "$NAMESPACE" >/dev/null 2>&1
}

# Backup PostgreSQL database
backup_postgresql() {
    log_info "Starting PostgreSQL database backup..."

    if uses_rds; then
        log_error "PostgreSQL in $NAMESPACE is Amazon RDS; this script only dumps in-cluster Postgres. Rely on RDS automated backups and snapshots instead."
        return 1
    fi
    local pod_name=$(kubectl get pods -n "$NAMESPACE" -l app=postgres -o jsonpath='{.items[0].metadata.name}')
    if [ -z "$pod_name" ]; then
        log_error "PostgreSQL pod not found"
        return 1
    fi

    local backup_file="$BACKUP_DIR/postgres_backup_${TIMESTAMP}.sql"

    log_info "Executing pg_dump on pod: $pod_name"
    kubectl exec -n "$NAMESPACE" "$pod_name" -- pg_dump -U raguser -d ragdb > "$backup_file"

    if [ $? -eq 0 ]; then
        log_success "PostgreSQL backup completed: $backup_file"

        # Compress backup
        gzip "$backup_file"
        log_success "PostgreSQL backup compressed: ${backup_file}.gz"

        # Upload to S3 if AWS credentials are available
        if aws sts get-caller-identity &>/dev/null; then
            local s3_key="backups/postgres/postgres_backup_${TIMESTAMP}.sql.gz"
            s3_upload "${backup_file}.gz" "$s3_key"
            log_success "PostgreSQL backup uploaded to S3: $s3_key"
        fi
    else
        log_error "PostgreSQL backup failed"
        return 1
    fi
}

# Backup Redis data
backup_redis() {
    log_info "Starting Redis data backup..."

    local pod_name=$(kubectl get pods -n "$NAMESPACE" -l app=redis -o jsonpath='{.items[0].metadata.name}')
    if [ -z "$pod_name" ]; then
        log_error "Redis pod not found"
        return 1
    fi

    local backup_file="$BACKUP_DIR/redis_backup_${TIMESTAMP}.rdb"

    log_info "Executing Redis BGSAVE on pod: $pod_name"
    kubectl exec -n "$NAMESPACE" "$pod_name" -- redis-cli BGSAVE

    # Wait for backup to complete
    log_info "Waiting for Redis backup to complete..."
    sleep 10

    # Copy the RDB file
    kubectl cp "$NAMESPACE/$pod_name:/data/dump.rdb" "$backup_file"

    if [ $? -eq 0 ]; then
        log_success "Redis backup completed: $backup_file"

        # Compress backup
        gzip "$backup_file"
        log_success "Redis backup compressed: ${backup_file}.gz"

        # Upload to S3 if AWS credentials are available
        if aws sts get-caller-identity &>/dev/null; then
            local s3_key="backups/redis/redis_backup_${TIMESTAMP}.rdb.gz"
            s3_upload "${backup_file}.gz" "$s3_key"
            log_success "Redis backup uploaded to S3: $s3_key"
        fi
    else
        log_error "Redis backup failed"
        return 1
    fi
}

# Backup Neo4j data
backup_neo4j() {
    log_info "Starting Neo4j data backup..."

    local pod_name=$(kubectl get pods -n "$NAMESPACE" -l app=neo4j -o jsonpath='{.items[0].metadata.name}')
    if [ -z "$pod_name" ]; then
        log_error "Neo4j pod not found"
        return 1
    fi

    local backup_file="$BACKUP_DIR/neo4j_backup_${TIMESTAMP}.cypher"

    log_info "Executing Neo4j dump on pod: $pod_name"
    kubectl exec -n "$NAMESPACE" "$pod_name" -- cypher-shell -u neo4j -p "$NEO4J_PASSWORD" "CALL apoc.export.cypher.all('/tmp/neo4j_backup.cypher', {})" || {
        log_warning "Neo4j apoc.export not available, trying alternative method..."

        # Alternative: Use neo4j-admin dump (requires access to Neo4j data directory)
        kubectl exec -n "$NAMESPACE" "$pod_name" -- neo4j-admin dump --database=neo4j --to=/tmp/neo4j_backup.dump
        kubectl cp "$NAMESPACE/$pod_name:/tmp/neo4j_backup.dump" "$BACKUP_DIR/neo4j_backup_${TIMESTAMP}.dump"
        backup_file="$BACKUP_DIR/neo4j_backup_${TIMESTAMP}.dump"
    }

    if [ $? -eq 0 ]; then
        log_success "Neo4j backup completed: $backup_file"

        # Compress backup
        gzip "$backup_file"
        log_success "Neo4j backup compressed: ${backup_file}.gz"

        # Upload to S3 if AWS credentials are available
        if aws sts get-caller-identity &>/dev/null; then
            local s3_key="backups/neo4j/neo4j_backup_${TIMESTAMP}.dump.gz"
            s3_upload "${backup_file}.gz" "$s3_key"
            log_success "Neo4j backup uploaded to S3: $s3_key"
        fi
    else
        log_error "Neo4j backup failed"
        return 1
    fi
}

# Backup Kubernetes configurations
backup_kubernetes_configs() {
    log_info "Starting Kubernetes configuration backup..."

    local config_dir="$BACKUP_DIR/k8s_configs_${TIMESTAMP}"
    mkdir -p "$config_dir"

    # Backup resources in namespace. Secrets are deliberately excluded: a
    # backup must never become a second, plaintext copy of every credential.
    log_info "Backing up resources (excluding Secrets) in namespace: $NAMESPACE"
    kubectl get all,configmaps,pvc,ingress,roles,rolebindings -n "$NAMESPACE" -o yaml > "$config_dir/all_resources.yaml"

    # Backup specific application configurations
    log_info "Backing up Helm releases"
    helm list -n "$NAMESPACE" -o yaml > "$config_dir/helm_releases.yaml"

    # Get Helm values for each release
    for release in $(helm list -n "$NAMESPACE" -q); do
        log_info "Backing up Helm values for release: $release"
        helm get values "$release" -n "$NAMESPACE" > "$config_dir/helm_values_${release}.yaml"
    done

    # Backup Custom Resource Definitions
    log_info "Backing up Custom Resource Definitions"
    kubectl get crds -o yaml > "$config_dir/crds.yaml"

    # Backup namespace configuration
    log_info "Backing up namespace configuration"
    kubectl get namespace "$NAMESPACE" -o yaml > "$config_dir/namespace.yaml"

    log_success "Kubernetes configuration backup completed: $config_dir"

    # Create archive and upload to S3
    tar -czf "$BACKUP_DIR/k8s_configs_${TIMESTAMP}.tar.gz" -C "$BACKUP_DIR" "k8s_configs_${TIMESTAMP}"
    rm -r -- "$config_dir"

    if aws sts get-caller-identity &>/dev/null; then
        local s3_key="backups/kubernetes/k8s_configs_${TIMESTAMP}.tar.gz"
        s3_upload "$BACKUP_DIR/k8s_configs_${TIMESTAMP}.tar.gz" "$s3_key"
        log_success "Kubernetes configuration backup uploaded to S3: $s3_key"
    fi
}

# Backup application files
backup_application_files() {
    log_info "Starting application files backup..."

    local backup_file="$BACKUP_DIR/application_files_${TIMESTAMP}.tar.gz"

    # Backup uploads directory from persistent volume claims
    local uploads_pvc=$(kubectl get pvc -n "$NAMESPACE" -l app=knowledge-graph-analytics -o jsonpath='{.items[0].metadata.name}')
    if [ -n "$uploads_pvc" ]; then
        log_info "Backing up PVC: $uploads_pvc"

        # Create temporary pod for PVC access
        kubectl run backup-pod --image=busybox --rm -i --restart=Never --namespace="$NAMESPACE" \
            --overrides='{
              "spec": {
                "containers": [{
                  "name": "backup",
                  "image": "busybox",
                  "command": ["sleep", "3600"],
                  "volumeMounts": [{
                    "name": "uploads",
                    "mountPath": "/data"
                  }]
                }],
                "volumes": [{
                  "name": "uploads",
                  "persistentVolumeClaim": {
                    "claimName": "'$uploads_pvc'"
                  }
                }]
              }
            }' &

        local backup_pod="backup-pod"

        # Wait for pod to be ready
        kubectl wait --for=condition=Ready pod/$backup_pod -n "$NAMESPACE" --timeout=300s

        # Copy files from PVC
        kubectl exec -n "$NAMESPACE" "$backup_pod" -- tar -czf /tmp/uploads_backup.tar.gz -C /data .
        kubectl cp "$NAMESPACE/$backup_pod:/tmp/uploads_backup.tar.gz" "$BACKUP_DIR/uploads_backup_${TIMESTAMP}.tar.gz"

        # Delete temporary pod
        kubectl delete pod "$backup_pod" -n "$NAMESPACE" --force --grace-period=0

        log_success "Application files backup completed: $BACKUP_DIR/uploads_backup_${TIMESTAMP}.tar.gz"

        # Upload to S3 if AWS credentials are available
        if aws sts get-caller-identity &>/dev/null; then
            local s3_key="backups/files/uploads_backup_${TIMESTAMP}.tar.gz"
            s3_upload "$BACKUP_DIR/uploads_backup_${TIMESTAMP}.tar.gz" "$s3_key"
            log_success "Application files backup uploaded to S3: $s3_key"
        fi
    else
        log_warning "No uploads PVC found for backup"
    fi
}

# Create Velero backup
create_velero_backup() {
    log_info "Creating Velero backup..."

    if command -v velero &> /dev/null; then
        local backup_name="knowledge-graph-backup-${TIMESTAMP}"

        log_info "Running Velero backup: $backup_name"
        # Needs Velero >= 1.10, which renamed --default-volumes-to-restic.
        # The command is the if condition: under set -e a bare failure would
        # exit before the error below is logged.
        if velero create backup "$backup_name" \
            --namespace "$VELERO_NAMESPACE" \
            --include-namespaces "$NAMESPACE" \
            --exclude-resources secrets \
            --default-volumes-to-fs-backup=false \
            --wait; then
            log_success "Velero backup completed: $backup_name"
        else
            log_error "Velero backup failed"
            return 1
        fi
    else
        log_warning "Velero not available, skipping Velero backup"
    fi
}

# Report a failure that happened after PostgreSQL may have been loaded.
fail_after_postgres() {
    local restored="$1" backup_id="$2" message="$3"
    if [ "$restored" -gt 0 ]; then
        log_error "$message. PostgreSQL WAS ALREADY RESTORED from $backup_id; do not re-run the restore against it. Finish the Redis step by hand."
    else
        log_error "$message"
    fi
}

# Restore from backup
restore_backup() {
    local backup_id="$1"

    if [ -z "$backup_id" ]; then
        log_error "Backup ID is required for restore"
        return 1
    fi
    # IDs are the TIMESTAMP used at backup time; reject anything else so it
    # cannot widen the S3 filter or escape the restore directory.
    if [[ ! "$backup_id" =~ ^[0-9]{8}_[0-9]{6}$ ]]; then
        log_error "Invalid backup ID '$backup_id' (expected YYYYMMDD_HHMMSS)"
        return 1
    fi

    log_info "Starting restore from backup: $backup_id"

    # Download backup files from S3. The directory holds plaintext dumps, so
    # it is removed when the script exits, whatever the outcome.
    local restore_dir="$BACKUP_DIR/restore_${backup_id}"
    RESTORE_DIR_TO_CLEAN="$restore_dir"
    trap 'rm -rf -- "${RESTORE_DIR_TO_CLEAN:-}"' EXIT
    mkdir -p "$restore_dir"

    # List available backups
    if aws sts get-caller-identity &>/dev/null; then
        log_info "Available backups in S3:"
        aws s3 ls "s3://$S3_BUCKET/backups/" --recursive

        # Uploads live at backups/<component>/<component>_backup_<id>.<ext>,
        # so download every component key for this id.
        log_info "Downloading backup files for: $backup_id"
        aws s3 sync "s3://$S3_BUCKET/backups/" "$restore_dir/" \
            --exclude "*" --include "*_${backup_id}.*"
    else
        log_error "AWS credentials not configured for restore"
        return 1
    fi

    if ! find "$restore_dir" -type f -name "*_${backup_id}.*" | grep -q .; then
        log_error "No backup files match ID $backup_id in s3://$S3_BUCKET/backups/; nothing restored"
        return 1
    fi

    # A full backup has all three; say loudly which ones this one lacks.
    local component
    for component in postgres redis neo4j; do
        if ! find "$restore_dir/$component" -type f -name "*_${backup_id}.*" 2>/dev/null | grep -q .; then
            if [ "$component" = "neo4j" ]; then
                log_warning "MISSING from backup $backup_id: neo4j (this script never restores neo4j; there is no dump to restore by hand either)"
            else
                log_warning "MISSING from backup $backup_id: $component (it will not be restored)"
            fi
        fi
    done

    local pg_file="$restore_dir/postgres/postgres_backup_${backup_id}.sql.gz"
    local redis_file="$restore_dir/redis/redis_backup_${backup_id}.rdb.gz"
    local redis_pod=""

    # Check the preconditions of every component before applying any of them,
    # so a failure cannot leave a half-restored system.
    log_info "Checking restore preconditions..."
    if [ -f "$pg_file" ]; then
        # This restores in-cluster Postgres only. On AWS the database is RDS,
        # which is restored from its own snapshots, not from this dump.
        if uses_rds; then
            log_error "PostgreSQL in $NAMESPACE is Amazon RDS (ExternalSecret aws-database-credentials exists). This script does not restore RDS: use an RDS snapshot or point-in-time restore. Nothing restored."
            return 1
        fi
        if ! kubectl get deployment postgres -n "$NAMESPACE" >/dev/null 2>&1; then
            log_error "No in-cluster deployment/postgres in $NAMESPACE; nothing restored"
            return 1
        fi
        if ! gzip -t "$pg_file"; then
            log_error "PostgreSQL dump is corrupt: $pg_file; nothing restored"
            return 1
        fi
        # The dump is a plain pg_dump without --clean: loading it into a
        # populated database collides with existing objects. Require an empty
        # target instead of guessing what to drop. Count base and partitioned
        # tables only: views and extension-owned tables do not make it populated.
        local table_count
        if ! table_count=$(kubectl exec -n "$NAMESPACE" deployment/postgres -- \
            psql -U raguser -d ragdb -tAc \
            "SELECT count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')"); then
            log_error "Cannot query the target PostgreSQL database; nothing restored"
            return 1
        fi
        table_count="${table_count//[[:space:]]/}"
        if [ "$table_count" != "0" ]; then
            log_error "Target database ragdb is not empty ($table_count tables). The dump has no DROP statements: scale the backend, workers and migrate job to 0, drop and recreate ragdb (see RUNBOOKS.md), then re-run. Nothing restored."
            return 1
        fi
    fi
    if [ -f "$redis_file" ]; then
        if ! gzip -t "$redis_file"; then
            log_error "Redis snapshot is corrupt: $redis_file; nothing restored"
            return 1
        fi
        redis_pod=$(kubectl get pods -n "$NAMESPACE" -l app=redis -o jsonpath='{.items[0].metadata.name}')
        if [ -z "$redis_pod" ]; then
            log_error "Redis pod not found in $NAMESPACE; nothing restored"
            return 1
        fi
    fi

    local restored=0

    # Restore PostgreSQL in one transaction that stops at the first error.
    if [ -f "$pg_file" ]; then
        log_info "Restoring PostgreSQL database..."
        if ! gunzip -c "$pg_file" | kubectl exec -i -n "$NAMESPACE" deployment/postgres -- \
            psql -U raguser -d ragdb -v ON_ERROR_STOP=1 --single-transaction; then
            log_error "PostgreSQL restore failed; the transaction was rolled back"
            return 1
        fi
        restored=$((restored + 1))
        log_success "PostgreSQL restore completed"
    fi

    # Stage the Redis snapshot in the pod. Loading it means replacing dump.rdb
    # while Redis is stopped, which depends on the deployment: left manual.
    if [ -f "$redis_file" ]; then
        local redis_rdb="$restore_dir/redis/redis_backup_${backup_id}.rdb"
        local redis_target="/tmp/redis_restore_${backup_id}.rdb"
        if ! gunzip -c "$redis_file" > "$redis_rdb"; then
            fail_after_postgres "$restored" "$backup_id" "Decompressing the Redis snapshot failed"
            return 1
        fi
        if ! kubectl cp "$redis_rdb" "$NAMESPACE/$redis_pod:$redis_target"; then
            fail_after_postgres "$restored" "$backup_id" "Copying the Redis snapshot into $redis_pod failed"
            return 1
        fi
        log_warning "Redis snapshot copied to $redis_pod:$redis_target but NOT loaded: stop Redis, replace dump.rdb in its data dir, then restart it."
    fi

    # Neo4j, Kubernetes configs and uploads are downloaded but not restored.
    local skipped
    skipped=$(find "$restore_dir" -type f -name "*_${backup_id}.*" \
        ! -path "$restore_dir/postgres/*" ! -path "$restore_dir/redis/*" | sed "s|^$restore_dir/||")
    if [ -n "$skipped" ]; then
        log_warning "SKIPPED, this script does not restore these components; restore them by hand:"
        printf '%s\n' "$skipped" | sed 's/^/    /'
    fi

    if [ "$restored" -eq 0 ]; then
        log_error "No component was restored for backup $backup_id"
        return 1
    fi
    log_success "Restored $restored component(s) from backup $backup_id. Re-create the infisical-universal-auth Secret (infrastructure/kubernetes/infisical-bootstrap.md) so the Infisical operator re-syncs the app Secrets, then verify the application."
}

# List available backups
list_backups() {
    log_info "Listing available backups..."

    if aws sts get-caller-identity &>/dev/null; then
        echo "=== S3 Backups ==="
        aws s3 ls "s3://$S3_BUCKET/backups/" --recursive | head -20

        if command -v velero &> /dev/null; then
            echo
            echo "=== Velero Backups ==="
            velero get backups --namespace "$VELERO_NAMESPACE"
        fi
    else
        log_error "AWS credentials not configured"
    fi
}

# Cleanup old backups
cleanup_old_backups() {
    local retention_days="${1:-30}"

    log_info "Cleaning up backups older than $retention_days days..."

    if aws sts get-caller-identity &>/dev/null; then
        # Clean up S3 backups
        local cutoff_date=$(date -d "$retention_days days ago" +%Y%m%d)

        aws s3 ls "s3://$S3_BUCKET/backups/" --recursive | \
        while read -r line; do
            local date_str=$(echo "$line" | awk '{print $1}')
            local file_path=$(echo "$line" | awk '{print $4}')

            # Convert date string to comparable format
            local file_date=$(date -d "$date_str" +%Y%m%d 2>/dev/null || echo "00000000")

            if [ "$file_date" -lt "$cutoff_date" ]; then
                log_info "Deleting old backup: $file_path"
                aws s3 rm "s3://$S3_BUCKET/$file_path"
            fi
        done

        log_success "Old backup cleanup completed"
    else
        log_error "AWS credentials not configured"
    fi
}

# Main execution
main() {
    case "${1:-backup}" in
        "backup")
            log_info "Starting full backup process..."
            check_dependencies
            create_backup_dir
            backup_postgresql
            backup_redis
            backup_neo4j
            backup_kubernetes_configs
            backup_application_files
            create_velero_backup
            log_success "Full backup process completed"
            ;;
        "restore")
            if [ -z "${2:-}" ]; then
                log_error "Backup ID is required for restore"
                echo "Usage: $0 restore <backup_id>"
                exit 1
            fi
            check_dependencies
            restore_backup "$2"
            ;;
        "list"|"ls")
            check_dependencies
            list_backups
            ;;
        "cleanup")
            local days="${2:-30}"
            check_dependencies
            cleanup_old_backups "$days"
            ;;
        "help"|"-h"|"--help")
            cat << EOF
Backup and Restore Script

Usage: $0 COMMAND [OPTIONS]

Commands:
    backup                   Perform full backup of all components
    restore <backup_id>      Restore from a specific backup
    list, ls                 List available backups
    cleanup [days]           Clean up old backups (default: 30 days)
    help                     Show this help message

Examples:
    $0 backup
    $0 restore 20231201_120000
    $0 list
    $0 cleanup 7

Components backed up:
    - PostgreSQL database
    - Redis cache
    - Neo4j graph database
    - Kubernetes configurations (Secrets are NOT included; restore them
      from the secret manager)
    - Velero cluster backup (needs Velero >= 1.10; also excludes Secrets:
      --exclude-resources secrets)
    - Application files

Environment:
    NAMESPACE            App namespace (default: multimodal-rag-system)
    VELERO_NAMESPACE     Namespace Velero runs in (default: velero)
    BACKUP_DIR           Local staging dir (default: a fresh mktemp -d, mode 0700)
    S3_BUCKET            Destination bucket (default: knowledge-graph-analytics-backups)
    BACKUP_KMS_KEY_ID    KMS key for SSE-KMS uploads (default: the aws/s3 key)

Important:
    - Every upload uses S3 server-side KMS encryption (--sse aws:kms)
    - Ensure AWS credentials are configured for S3 operations
    - Verify backups are created successfully
    - Test restore procedures regularly
    - Monitor backup processes for failures

EOF
            ;;
        *)
            log_error "Unknown command: $1"
            echo "Run '$0 help' for usage information"
            exit 1
            ;;
    esac
}

# Run main unless sourced (tests source the script to call one function).
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
