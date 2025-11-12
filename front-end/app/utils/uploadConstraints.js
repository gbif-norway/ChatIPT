export const ALLOWED_FILE_EXTENSIONS = [
  '.csv',
  '.tsv',
  '.txt',
  '.xlsx',
  '.xls',
  '.xlsm',
  '.xlsb',
  '.ods',
  '.newick',
  '.nex',
  '.nexus',
  '.tre',
  '.tree',
  '.nwk'
];

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

