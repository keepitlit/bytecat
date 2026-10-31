# Running `bytecat audit` on a host with no Python

`bytecat audit` has to read real bytes, so it runs on the host that holds them. Some NAS
operating systems ship without a Python interpreter and without a package manager you can
use. The trick that works: any container on that host that *does* contain Python can run the
audit against a bind-mounted directory.

1. Stage the package and a copy of the catalog on the host:

   ```sh
   ssh nas 'mkdir -p /mnt/cache/bytecat-audit/bytecat'
   rsync -a bytecat/ nas:/mnt/cache/bytecat-audit/bytecat/
   rsync -a ~/.local/share/bytecat/catalog.db nas:/mnt/cache/bytecat-audit/
   ```

2. Find a container with Python 3.9+ (`docker exec <name> python3 --version`). Many media
   and download containers have one.

3. Run the audit inside it, mounting the staging directory and the tree under test. Mount
   the tree at the same path it was scanned from: `--path` must be at or under a scan root
   the catalog knows, or the audit stops with `not catalogued`.

   ```sh
   docker run --rm \
     -v /mnt/cache/bytecat-audit:/work \
     -v /mnt/user/media:/mnt/user/media:ro \
     -e BYTECAT_HOME=/work \
     -w /work \
     <image-with-python> \
     python3 -m bytecat.audit dupes --path /mnt/user/media
   ```

4. Copy the report back from `/mnt/cache/bytecat-audit/reports/` and **delete the staging
   directory**: a stale copy of the catalog is a list of every file you own, sitting on a
   NAS share.

The audit imports nothing from the catalog code path (see `bytecat/audit.py`), so the
staged copy only needs `audit.py`, `db.py` and `schema.sql`, but copying the package is
simpler and the size is trivial.
