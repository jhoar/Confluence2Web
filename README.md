# Confluence2Web

Convert all Confluence pages in a space to a hierarchy of HTML pages with built-in navigation.

## Requirements

- Python 3.10+
- `pip install -r requirements.txt`

## Usage

```bash
python confluence_to_html.py \
  --base-url "https://your-domain.atlassian.net/wiki" \
  --space-id "SPACEKEY" \
  --token "YOUR_PERSONAL_ACCESS_TOKEN" \
  --output-dir "exported_html"
```

### Notes

- `--space-id` accepts a space key. Numeric space ID is also attempted for Confluence Cloud.
- By default `--cloud` is enabled. Use `--no-cloud` for Data Center/Server.
- Generated output includes:
  - Hierarchical folders per page
  - `index.html` in each page folder
  - Sidebar tree navigation
  - Breadcrumbs
  - Previous/Up/Next page controls
