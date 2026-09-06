# Nextcloud WebDAV — Hosting Files for Jellyfin

The user's Nextcloud at `files.outsidecontext.solutions` uses LDAP authentication (same hermes credentials as all other services). Used primarily to host IPTV M3U playlists.

## Key Quirk: UUID Username

The Nextcloud WebDAV path uses a **UUID as the user identifier**, not the LDAP username. Trying `/remote.php/dav/files/hermes/` returns `404: Principal with name hermes not found`.

### How to Find the UUID

```bash
curl -s -u 'hermes:<LDAP_PASSWORD>' \
  "https://files.outsidecontext.solutions/ocs/v1.php/cloud/user" \
  -H "OCS-APIRequest: true"
```

The `<id>` field in the response is the UUID to use in WebDAV paths.

## WebDAV Upload

```bash
curl -s -u 'hermes:<LDAP_PASSWORD>' \
  -T /path/to/local/file \
  "https://files.outsidecontext.solutions/remote.php/dav/files/<UUID>/<filename>"
```

## Creating a Public Share

```bash
curl -s -u 'hermes:<LDAP_PASSWORD>' \
  -X POST \
  -H "OCS-APIRequest: true" \
  -d 'shareType=3&path=/<filename>&permissions=1' \
  "https://files.outsidecontext.solutions/ocs/v2.php/apps/files_sharing/api/v1/shares"
```

The response contains a `<token>` field. The public download URL becomes:
```
https://files.outsidecontext.solutions/s/<token>/download
```

### Updating an Existing Share

Delete the old share first (find the share ID via `GET .../shares`), then create a new one:

```bash
# List shares
curl -s -u 'hermes:<LDAP_PASSWORD>' \
  "https://files.outsidecontext.solutions/ocs/v2.php/apps/files_sharing/api/v1/shares" \
  -H "OCS-APIRequest: true"

# Delete share by ID
curl -s -u 'hermes:<LDAP_PASSWORD>' \
  -X DELETE \
  -H "OCS-APIRequest: true" \
  "https://files.outsidecontext.solutions/ocs/v2.php/apps/files_sharing/api/v1/shares/<ID>"
```

## Groups

The user belongs to `media` and `pirates` groups.