// Where the installed OpenClaw package lives. Set OPENCLAW_PACKAGE_DIR to override; by default it
// is the global npm install next to the node binary running this helper (the service starts these
// helpers with the node found beside the configured `openclaw` binary).
import {dirname, resolve} from 'node:path';
export const OPENCLAW = process.env.OPENCLAW_PACKAGE_DIR || resolve(dirname(process.execPath), '..', 'lib', 'node_modules', 'openclaw');
export const gatewayRuntime = () => import(`${OPENCLAW}/dist/plugin-sdk/gateway-runtime.js`);
