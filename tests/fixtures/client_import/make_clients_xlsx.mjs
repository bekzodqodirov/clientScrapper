// Writes clients.xlsx from clients_book.json the way the ERP's «Mijozlar → XLSX»
// does (fullprompt agent-feed d138b3bc, src/app/api/clients/xlsx/route.ts):
// one sheet «Clients», a bold header row of CORE_TITLES + the custom field's
// label, the column widths, then one row per card — the phones list joined
// with ", ", Active as «✓» or empty. Same writer: exceljs 4.4.0, the ERP's own
// node_modules. Run from this directory:
//
//   NODE_PATH=/home/user/fullprompt/node_modules node make_clients_xlsx.mjs
//
// The creation stamps are fixed; the zip's own entry times are not, so a
// re-run changes the sha256 the README records.
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';

const require = createRequire(import.meta.url);
const ExcelJS = require('exceljs');

const book = JSON.parse(readFileSync(new URL('./clients_book.json', import.meta.url), 'utf8'));
const visible = [
  { key: 'code', title: 'Code' },
  { key: 'name', title: 'Name' },
  { key: 'manager', title: 'Manager' },
  { key: 'phone', title: 'Phones' },
  { key: 'active', title: 'Active' },
  { key: 'custom', title: book.custom_field },
];

const workbook = new ExcelJS.Workbook();
workbook.created = new Date('2026-10-24T00:00:00Z');
workbook.modified = new Date('2026-10-24T00:00:00Z');
const sheet = workbook.addWorksheet('Clients');
const head = sheet.addRow(visible.map((column) => column.title));
head.font = { bold: true };
sheet.columns = visible.map((column) => ({ width: column.key === 'name' ? 32 : 18 }));
for (const card of book.cards) {
  const row = {
    code: card.code,
    name: card.name,
    manager: card.manager,
    phone: card.phones.join(', '),
    active: card.active,
    custom: card.custom,
  };
  sheet.addRow(
    visible.map((column) => {
      if (column.key === 'active') return row.active ? '✓' : '';
      const value = row[column.key];
      return value === null || value === undefined ? '' : value;
    }),
  );
}
const buffer = await workbook.xlsx.writeBuffer();
writeFileSync(new URL('./clients.xlsx', import.meta.url), Buffer.from(buffer));
