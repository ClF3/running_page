import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const polyline = require('@mapbox/polyline');

export const decode = polyline.decode;
export const encode = polyline.encode;
export default polyline;
