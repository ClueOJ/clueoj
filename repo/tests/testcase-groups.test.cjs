const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const context = vm.createContext({});
vm.runInContext(readFileSync('resources/testcase-groups.js', 'utf8'), context);
const plan = (raw, n, limit = 9999) => JSON.parse(JSON.stringify(context.planTestcaseGroups(raw, n, limit)));
const errorOf = (fn) => { try { fn(); } catch (e) { return e; } return null; };
assert.deepEqual(plan('20 30 30 20', 50), [
    {points: 20, count: 10}, {points: 30, count: 15}, {points: 30, count: 15}, {points: 20, count: 10},
]);
assert.deepEqual(plan(' 2\t3\n3 2 ', 50).map(g => g.count), [10, 15, 15, 10]);
assert.deepEqual(plan('7', 50), [{points: 7, count: 50}]);
assert.deepEqual(plan('', 50), []);
for (const raw of ['0 10', '-1 2', '1.5 2', 'NaN', 'Infinity', '2,3', '2e1', '2147483648'])
    assert.equal(errorOf(() => plan(raw, 50)).code, 'invalid_points', raw);
const notDivisible = errorOf(() => plan('33 33 34', 50));
assert.equal(notDivisible.code, 'not_divisible');
assert.equal(notDivisible.params.join(','), '1,50,33,100');
assert.equal(errorOf(() => plan('1 1 1', 2)).code, 'not_divisible');
assert.equal(errorOf(() => plan('1', 0)).code, 'no_tests');
assert.equal(errorOf(() => plan('20 30 30 20', 50, 57)).code, 'too_many_rows');
assert.equal(plan('20 30 30 20', 50, 58).reduce((n, g) => n + g.count, 0), 50);
assert.deepEqual(plan('2147483647 2147483647', 2), [
    {points: 2147483647, count: 1}, {points: 2147483647, count: 1},
]);
const structured = context.makeGroupError('too_many_tests', [100]);
assert.equal(structured.code, 'too_many_tests');
assert.deepEqual(structured.params, [100]);
console.log('Testcase group validation passed.');
