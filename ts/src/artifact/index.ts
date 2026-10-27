// The `@action-state-group/capsule-emit/artifact` subpath: the storage-driver-free
// neutral record model. Import `./artifact/sqlite` or `./artifact/mysql` for a
// backend; each links only its own database driver.
export * from "./record.js";
export {
  DEFAULT_FIND_LIMIT,
  MAX_FIND_LIMIT,
  MAX_INDEX_PATHS,
  isIndexStore,
  type IndexStore,
  type Page,
  type Query,
} from "./lookup.js";
