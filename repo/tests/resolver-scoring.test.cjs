const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function resolver(hidden, points, precision = 3) {
    const context = {
        console: {log() {}},
        $: {extend: (_, target, source) => structuredClone(source)},
    };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../resources/resolver.js'), 'utf8'), context);
    const maximum = hidden.length ? {'1': 0.3, '2': 0.7} : {'1': 1};
    const result = new context.Resolver([Object.keys(maximum).length], [hidden], {'1': maximum}, {
        '1': {name: 'decimal', last_submission: 0, problems: {
            '1': {points, frozen_points: points},
        }},
    }, precision);
    result.calcOperations();
    return result;
}

test('visible decimal points keep the contest precision', () => {
    const result = resolver([], {'1': 0.333});
    assert.equal(result.rankarr[0].score, 0.333);
    assert.equal(result.rankarr[0].problem[1].old_point, 0.333);
    assert.equal(result.check[0][0][0], 1);
});

test('revealing a hidden decimal group updates the score without rounding to two digits', () => {
    const result = resolver([2], {'1': 0.111, '2': 0.222});
    assert.equal(result.rankarr[0].score, 0.111);
    assert.equal(result.operation(0, 1, 2), true);
    assert.equal(result.rankarr[0].score, 0.333);
    assert.equal(result.rankarr[0].problem[1].old_point, 0.333);
});

test('automatic reveal finds a submitted problem and its hidden group', () => {
    const result = resolver([2], {'1': 0.111, '2': 0.222});
    assert.equal(result.next_operation(), true);
    assert.equal(result.rankarr[0].score, 0.333);
});

test('rounding respects other configured contest precisions', () => {
    assert.equal(resolver([], {'1': 0.123456}, 6).rankarr[0].score, 0.123456);
    assert.equal(resolver([], {'1': 1}, 0).roundScore(1.4), 1);
});
