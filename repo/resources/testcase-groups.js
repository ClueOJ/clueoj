/* Build groups using exact integer arithmetic; never round or omit tests.
   Errors are structured ({code, params}) so the page can localize them. */
function makeGroupError(code, params) {
    var error = new Error(code);
    error.code = code;
    error.params = params || [];
    return error;
}

function planTestcaseGroups(raw, count, rowLimit) {
    if (!raw.trim()) return [];
    var tokens = raw.trim().split(/\s+/);
    if (tokens.some(function (token) {
        return !/^[0-9]+$/.test(token) || Number(token) < 1 || Number(token) > 2147483647;
    })) throw makeGroupError('invalid_points');
    if (!Number.isSafeInteger(count) || count < 1)
        throw makeGroupError('no_tests');
    var points = tokens.map(Number);
    var total = points.reduce(function (sum, point) { return sum + BigInt(point); }, 0n);
    var groups = points.map(function (point, index) {
        var numerator = BigInt(count) * BigInt(point);
        if (numerator % total !== 0n)
            throw makeGroupError('not_divisible', [index + 1, count, point, total.toString()]);
        return {points: point, count: Number(numerator / total)};
    });
    if (count + groups.length * 2 > rowLimit)
        throw makeGroupError('too_many_rows', [rowLimit]);
    return groups;
}
