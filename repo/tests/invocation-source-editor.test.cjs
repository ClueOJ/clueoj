const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function selection() {
    const context = vm.createContext({});
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../resources/invocation-source-editor.js'), 'utf8'), context);
    return vm.runInContext('new InvocationCellSelection()', context);
}

test('Click toggles a cell without clearing other selected cells', () => {
    const cells = selection();
    cells.toggle(0, 0);
    cells.toggle(3, 2);
    assert.equal(cells.selected.size, 2);
    cells.toggle(0, 0);
    assert.equal(cells.has(0, 0), false);
    assert.equal(cells.has(3, 2), true);
});

test('Shift selects an inclusive rectangle backwards and preserves disjoint selections', () => {
    const cells = selection();
    cells.toggle(0, 0);
    cells.toggle(3, 4);
    cells.toggle(1, 2, true);
    assert.equal(cells.selected.size, 10);
    for (let row = 1; row <= 3; row++) {
        for (let column = 2; column <= 4; column++) assert.equal(cells.has(row, column), true);
    }
    assert.equal(cells.has(0, 0), true);
    assert.equal(cells.has(0, 2), false);
});

test('Successive Shift selections use the last ordinary click as their anchor', () => {
    const cells = selection();
    cells.toggle(0, 0);
    cells.toggle(1, 1, true);
    cells.toggle(2, 0, true);
    assert.equal(cells.selected.size, 5);
    assert.equal(cells.has(2, 0), true);
    assert.equal(cells.has(2, 1), false);
});

test('Clearing removes the anchor so a later Shift click selects only its cell', () => {
    const cells = selection();
    cells.toggle(0, 0);
    cells.toggle(3, 3, true);
    cells.clear();
    cells.toggle(4, 4, true);
    assert.equal(cells.selected.size, 1);
    assert.equal(cells.has(4, 4), true);
});
