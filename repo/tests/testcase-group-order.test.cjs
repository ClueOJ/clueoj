// Exercise the page's actual regrouping function with a small form/DOM adapter.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const makeRow = (id, type, deleted = false) => ({id, visible: true, fields: Object.fromEntries(
    Object.entries({type, DELETE: deleted, points: 7, is_pretest: true, checker: 'custom',
        checker_args: 'config', generator_args: 'seed', input_file: id + '.in', output_file: id + '.out', order: 1})
        .map(([key, value]) => [key, {key, value}]))});
let rows = [makeRow('old-start', 'S'), makeRow('10', 'C'), makeRow('2', 'C'),
    makeRow('old-end', 'E'), makeRow('deleted', 'C', true), makeRow('9', 'C'), makeRow('1', 'C')];
const originalCases = rows.filter(r => r.fields.type.value === 'C' && !r.fields.DELETE.value);
const originalData = originalCases.map(r => JSON.stringify(Object.fromEntries(
    Object.entries(r.fields).filter(([key]) => key !== 'order'))));
const input = {value: '1 1'}, total = {value: rows.length}, status = {};
const body = {body: true}, table = {table: true};
class Query extends Array {
    find(selector) {
        if (this[0]?.table) return $(body);
        const names = [...selector.matchAll(/\[id\$="-([^"]+)"\]/g)].map(m => m[1]);
        return new Query(...this.flatMap(r => names.map(n => r.fields?.[n]).filter(Boolean)));
    }
    children() { return new Query(...rows); }
    filter(fn) { return new Query(...Array.from(this).filter((r, i) => fn.call(r, i))); }
    toArray() { return Array.from(this); }
    val(value) { if (value === undefined) return this[0]?.value; this.forEach(x => x.value = value); return this; }
    prop(key, value) { return this.val(value); }
    change() { return this; }
    hide() { this.forEach(x => x.visible = false); return this; }
    show() { this.forEach(x => x.visible = true); return this; }
    addClass() { return this; }
    removeClass() { return this; }
    text(value) { this.forEach(x => x.text = value); return this; }
    siblings() { return new Query(); }
    last() { return $(this[this.length - 1]); }
    append(row) { rows = rows.filter(r => r !== row); rows.push(row); return this; }
    click() { rows.push(makeRow('new-' + total.value++, 'C')); return this; }
}
function $(value) {
    if (value === '#testcase-group-points') return new Query(input);
    if (value === '#testcase-group-status') return new Query(status);
    if (typeof value === 'string') return new Query({});
    return new Query(value);
}
const context = vm.createContext({$, $table: $(table), $total: $(total), window: {testcase_limit: 100},
    handle_table_reorder() {}, appliedGroupPoints: null,
    format_group_error: e => (e && e.code ? e.code + '|' + (e.params || []).join('/') : String(e)),
    format_group_summary: groups => groups.map(g => g.points + '/' + g.count).join(';'),
    format_ungrouped_keep_order: () => 'UNGROUPED'});
vm.runInContext(fs.readFileSync('resources/testcase-groups.js', 'utf8'), context);
const template = fs.readFileSync('templates/problem/data.html', 'utf8');
const start = template.indexOf('            function group_current_testcases()');
const end = template.indexOf("            $('#case-table').closest('form')", start);
vm.runInContext(template.slice(start, end), context);
const active = () => rows.filter(r => !r.fields.DELETE.value);
const types = () => active().map(r => r.fields.type.value).join('');
context.group_current_testcases();
assert.equal(types(), 'SCCESCCE');
assert.deepEqual(active().filter(r => r.fields.type.value === 'C'), originalCases);
assert.deepEqual(originalCases.map(r => JSON.stringify(Object.fromEntries(
    Object.entries(r.fields).filter(([key]) => key !== 'order')))), originalData);
assert.equal(active().filter(r => r.fields.type.value === 'S').map(r => r.fields.points.value).join(','), '1,1');
const beforeInvalid = JSON.stringify(rows);
input.value = '1 2';
context.group_current_testcases();
assert.equal(JSON.stringify(rows), beforeInvalid, 'Invalid division must not mutate rows');
assert.match(status.text, /not_divisible\|1\/4\/1\/3/);
input.value = '4';
context.group_current_testcases();
assert.equal(types(), 'SCCCCE');
assert.equal(total.value, 9, 'Reuse existing markers on repeated grouping');
input.value = '';
context.group_current_testcases();
assert.equal(types(), 'CCCC');
assert.deepEqual(active(), originalCases);
assert.ok(rows.filter(r => ['S', 'E'].includes(r.fields.type.value)).every(r => r.fields.DELETE.value && !r.visible));
console.log('Current order, test settings, deletion, regrouping, and invalid-input checks passed.');
