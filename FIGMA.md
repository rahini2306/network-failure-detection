# SIGNAL UI design

[Open the editable Figma design](https://www.figma.com/design/yY6cotYgA0JcVByKYfCoHV?node-id=8-48)

The file includes the final desktop dashboard, a tablet layout, reusable status cards and remove controls, color/spacing variables, and typography styles. Interface text, tables, charts, and layout frames are editable. The decorative dot-matrix canvas is a discrete image asset. Figma shows a design snapshot; live monitoring runs in the Flask application.

## Interface controls

- Click a status card to filter the device table.
- Search devices by name or IP address.
- Click a device name for its latest status, RTT, packet loss, and last check.
- Choose “View response chart” to inspect its trend.
- Filter activity by failures or recoveries.
- Refresh manually, or pause/resume dashboard updates. Pausing the display does not stop the monitor.
- Expand probe history and export CSV reports.

Inter and IBM Plex Mono fonts are bundled locally under `static/fonts/`.
