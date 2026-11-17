export const TABLE_FILE_EXTENSIONS = ['.csv', '.tsv', '.txt', '.xlsx', '.xls', '.xlsm', '.xlsb', '.ods'];
export const TREE_FILE_EXTENSIONS = ['.newick', '.nwk', '.nex', '.nexus', '.tre', '.tree'];

export const ALLOWED_FILE_EXTENSIONS = [...TABLE_FILE_EXTENSIONS, '.pdf', ...TREE_FILE_EXTENSIONS];

// Files a Darwin Core Archive upload may contain: the ZIP itself, or its loose tables and XML files.
export const ARCHIVE_FILE_EXTENSIONS = ['.zip', '.dwca', '.csv', '.tsv', '.txt', '.xml'];

export const ACCEPT_INPUT_EXTENSIONS = ALLOWED_FILE_EXTENSIONS.join(',');

export const isExtensionAllowed = (filename) => {
  if (typeof filename !== 'string') {
    return false;
  }
  const lastDot = filename.lastIndexOf('.');
  if (lastDot === -1) {
    return false;
  }
  const extension = filename.slice(lastDot).toLowerCase();
  return ALLOWED_FILE_EXTENSIONS.includes(extension);
};
