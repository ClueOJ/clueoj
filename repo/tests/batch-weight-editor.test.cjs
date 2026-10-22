const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function editor(rows) {
    class Query extends Array {
        each(callback) { this.forEach((row, index) => callback.call(row, index)); return this; }
        find(selector) {
            if (selector === '.batch-weight-label') return new Query(...this.map(row => row.label));
            const name = selector.includes('batch_type') ? 'batch_type' : selector.includes('pretest') ? 'pretest' : 'points';
            return new Query(...this.map(row => row[name]));
        }
        toggle(visible) { this.forEach(field => { field.visible = visible; }); return this; }
        val(value) { if (value === undefined) return this[0].value; this.forEach(field => { field.value = value; }); return this; }
        attr(name, value) {
            if (value === undefined) return this[0][name];
            this.forEach(field => { field[name] = value; }); return this;
        }
        removeAttr(name) { this.forEach(field => { delete field[name]; }); return this; }
    }
    const $ = row => new Query(row);
    const template = fs.readFileSync(path.join(__dirname, '../templates/problem/data.html'), 'utf8');
    const start = template.indexOf('            function handle_table_reorder()');
    const end = template.indexOf('            function try_parse_json(', start);
    const context = vm.createContext({$, $table: {find: () => new Query(...rows)}});
    vm.runInContext(template.slice(start, end), context);
    return () => context.handle_table_reorder();
}

function row(type, points = '', mode = 'sum') {
    return {'data-type': type, points: {value: points}, batch_type: {value: mode}, pretest: {}, label: {}};
}

test('Sum exposes individual weights and fills blank weights with one', () => {
    const rows = [row('S', 90), row('C'), row('C', 2), row('E')];
    editor(rows)();
    assert.equal(rows[1].points.visible, true);
    assert.equal(rows[1].points.value, 1);
    assert.equal(rows[2].points.value, 2);
    assert.equal(rows[1].label.visible, true);
    assert.equal(rows[1].pretest.visible, false);
});

test('Min hides weight controls and switching back to Sum preserves values', () => {
    const rows = [row('S', 90, 'min'), row('C', 2), row('E')];
    const update = editor(rows);
    update();
    assert.equal(rows[1].points.visible, false);
    assert.equal(rows[1].label.visible, false);
    rows[0].batch_type.value = 'sum';
    update();
    assert.equal(rows[1].points.visible, true);
    assert.equal(rows[1].points.value, 2);
});

test('Standalone points and pretest controls remain visible after a batch', () => {
    const rows = [row('S', 90), row('C', 1), row('E'), row('C', 7)];
    editor(rows)();
    assert.equal(rows[3].points.visible, true);
    assert.equal(rows[3].points.value, 7);
    assert.equal(rows[3].pretest.visible, true);
    assert.equal(rows[3].label.visible, false);
});

function changeType(previousType, mode, nextType) {
    const template = fs.readFileSync(path.join(__dirname, '../templates/problem/data.html'), 'utf8');
    const marker = '$tr.find(\'select[id$="-type"]\').change(function () {';
    const start = template.indexOf(marker) + marker.length;
    const end = template.indexOf('}).change();', start);
    const batch = {value: mode};
    const type = {value: nextType};
    const inert = {toggle() { return this; }, siblings() { return this; }, hide() { return this; },
                   find() { return this; }};
    const $tr = {
        previousType,
        find() {
            return {toggle: () => inert,
                    val(value) { if (value === undefined) return batch.value; batch.value = value; return this; }};
        },
        attr(name, value) {
            if (value === undefined) return this.previousType;
            this.previousType = value;
            return this;
        },
        nextUntil: () => inert,
        prevAll: () => ({length: 0}),
    };
    vm.runInNewContext('(function () {' + template.slice(start, end) + '}).call(type);', {
        $tr, type, $: field => ({val: () => field.value}),
        $opts: inert, $files: inert, $checker: inert, handle_table_reorder() {},
    });
    return batch.value;
}

test('Changing a newly added case to Batch start selects Sum even with a stale Min selector', () => {
    assert.equal(changeType('C', 'min', 'S'), 'sum');
});

test('Initializing or refreshing a saved Min batch keeps its selected policy', () => {
    assert.equal(changeType('S', 'min', 'S'), 'min');
});

test('Converting an end marker to a new Batch start defaults to Sum', () => {
    assert.equal(changeType('E', 'min', 'S'), 'sum');
});
