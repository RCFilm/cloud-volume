import posixpath
from collections import namedtuple
import json
import re
import requests
from six.moves import urllib

import numpy as np

from ... import exceptions
from ... import paths
from ...secrets import chunkedgraph_credentials
from ..precomputed import PrecomputedMetadata

VERSION_ORDERING = [  
  '1.0', 'v1'
]
VERSION_MAP = {
  version: i for i, version in enumerate(VERSION_ORDERING)
}

uint64 = np.uint64
GrapheneLabel = namedtuple('GrapheneLabel', ('level', 'x', 'y', 'z', 'segid'))


class GrapheneApiVersion():
  def __init__(self, version):
    self.version = version.lower()
    if self.version == 'table':
      self.version = VERSION_ORDERING[-1]
    elif self.version not in VERSION_MAP:
      raise ValueError("Unknown Graphene API version {}".format(self.version))

  def __eq__(self, rhs):
    return self.version == rhs.version
  def __ne__(self, rhs):
    return self.version != rhs.version
  def __lt__(self, rhs):
    return self.sequence_number() < rhs.sequence_number()
  def __gt__(self, rhs):
    return self.sequence_number() > rhs.sequence_number()
  def __le__(self, rhs):
    return self.sequence_number() <= rhs.sequence_number()
  def __ge__(self, rhs):
    return self.sequence_number() >= rhs.sequence_number()
  def __str__(self):
    return self.version
  def __repr__(self):
    return "GrapheneApiVersion('{}')".format(self.version)

  def sequence_number(self):
    return VERSION_MAP[self.version]

  def path(self, graphene_path):
    if self.version == '1.0':
      return self.legacy_path(graphene_path)
    return self.api_vx_path(graphene_path)

  def table_path(self, graphene_path):
    return posixpath.join(graphene_path.modality, 'table', graphene_path.dataset)

  def legacy_path(self, graphene_path):
    """All /segmentation/1.0/$DATASET paths"""
    return posixpath.join(graphene_path.modality, '1.0', graphene_path.dataset)

  def api_vx_path(self, graphene_path):
    """
    All /segmentation/api/v1/$DATASET paths.

    As of Feb. 2020, these were the latest paths.
    """
    return posixpath.join( 
      graphene_path.modality, 'api', self.version, 'table', graphene_path.dataset
    )

class GrapheneMetadata(PrecomputedMetadata):
  def __init__(self, cloudpath, use_https=False, use_auth=True, auth_token=None, *args, **kwargs):
    self.server_url = cloudpath.replace('graphene://', '')
    self.server_path = extract_graphene_path(self.server_url)
    self.use_https = use_https
    self.auth_header = None
    self.spatial_index = None
    if use_auth:
      token = None
      if chunkedgraph_credentials:
        token = chunkedgraph_credentials["token"]
      if auth_token:
        token = auth_token
      self.auth_header = {
        "Authorization": "Bearer %s" % token
      }
    super(GrapheneMetadata, self).__init__(cloudpath, *args, **kwargs)

    version = self.server_path.version
    if version == 'table':
      version = self.supported_api_versions[-1].version

    self.api_version = GrapheneApiVersion(version)

  def supports_api(self, version):
    return GrapheneApiVersion(version) in self.supported_api_versions

  @property  
  def supported_api_versions(self):
    versions = [ 
      GrapheneApiVersion(VERSION_ORDERING[i]) \
      for i in self.info['app']['supported_api_versions'] 
    ]
    versions.sort(key=lambda ver: ver.sequence_number())
    return versions

  @property
  def base_path(self):
    path = self.server_path
    if path.subdomain is None:
      return path.scheme + '://' + path.domain + '/'   
    return path.scheme + '://' + path.subdomain + '.' + path.domain + '/' 

  @property
  def table_path(self):
    return posixpath.join(self.base_path, self.server_path.modality, 'table', self.server_path.dataset)

  @property
  def info_path(self):
    """e.g. https://SUBDOMAIN.dynamicannotationframework.com/segmentation/table/DATASET/info"""
    return posixpath.join(self.table_path, 'info')

  def fetch_info(self):
    """
    Reads info from chunkedgraph endpoint and extracts relevant information
    """
    r = requests.get(self.info_path, headers=self.auth_header)
    r.raise_for_status()
    return r.json()

  @property
  def mesh_path(self):
    if 'mesh' in self.info:
      return self.info['mesh']
    return 'mesh'

  @property
  def cloudpath(self):
    data_dir = self.info['data_dir']
    if self.use_https:
      data_dir = paths.to_https_protocol(data_dir)
    return data_dir

  def decode_label(self, label):
    level = self.decode_level(label)
    x,y,z = self.decode_chunk_position(label)
    segid = self.decode_segid(label)
    return GrapheneLabel(level, x, y, z, segid)

  def decode_segid(self, label):
    label = uint64(label)
    level = self.decode_level(label)
    segid_bits = self.level_segid_bits(level)

    mask = uint64(0)
    for _ in range(segid_bits):
      mask |= uint64(1)
      mask = mask << uint64(1)

    return label & mask

  def decode_chunk_position(self, label):
    label = uint64(label)
    level = self.decode_level(label)
    ct = self.spatial_bit_count(level)
    label = label & uint64(0x00ffffffffffffff)
    masks = self.spatial_bit_masks(level)
    segid_bits = self.level_segid_bits(level)

    x = (label & masks[0]) >> uint64(segid_bits + 2 * ct)
    y = (label & masks[1]) >> uint64(segid_bits + 1 * ct)
    z = (label & masks[2]) >> uint64(segid_bits + 0 * ct)

    return (x,y,z)

  def level_segid_bits(self, level):
    ct = self.spatial_bit_count(level)
    return 64 - self.n_bits_for_layer_id - 3 * ct

  def decode_level(self, label):
    return (uint64(label) & uint64(0xff00000000000000)) >> uint64(64 - self.n_bits_for_layer_id)

  def decode_chunk_id(self, label):
    label = uint64(label)

    level = self.decode_level(label)
    ct = self.spatial_bit_count(level)

    segid_bits = self.level_segid_bits(level)
    label = label & uint64(0x00ffffffffffffff)
    return label >> uint64(segid_bits)

  def spatial_bit_masks(self, level):
    ct = self.spatial_bit_count(level)

    mask = uint64(0x0000000000000000)
    for _ in range(ct):
      mask |= uint64(1)
      mask = mask << uint64(1)

    segid_bits = 64 - self.n_bits_for_layer_id - 3 * ct

    return [
      mask << uint64(segid_bits + 2 * ct),
      mask << uint64(segid_bits + 1 * ct),
      mask << uint64(segid_bits + 0 * ct)
    ]

  def spatial_bit_count(self, level):
    """
    64-bit labels

    8-bit  chunk coord
    layer | X | Y | Z | segid

    This method returns how many bits in X,Y,Z
    """
    return int(self.info['graph']['spatial_bit_masks'][str(level)])

  @property
  def n_bits_for_layer_id(self):
    return int(self.info['graph'].get('n_bits_for_layer_id', 8))

  @property
  def n_layers(self):
    return int(self.info['graph']['n_layers'])

  @property
  def graph_chunk_size(self):
    return self.info['graph']['chunk_size']
  
  @property
  def mesh_chunk_size(self):
    # TODO: add this as new parameter to the info as it can be different from the chunkedgraph chunksize
    return self.graph_chunk_size

  @property
  def manifest_endpoint(self):
    pth = self.server_path
    pth = GraphenePath(
      pth.scheme, pth.subdomain, pth.domain, 
      'meshing', pth.version, pth.dataset
    )

    url = self.api_version.path(pth)
    return posixpath.join(self.base_path, url, 'manifest')

  @property
  def chunks_start_at_voxel_offset(self):
    """
    Boolean property specifying whether ChunkedGraph chunks begin
    at voxel offset or at origin.
    """
    if 'chunks_start_at_voxel_offset' in self.info:
      return self.info["chunks_start_at_voxel_offset"]
    return False

  @property
  def mesh_metadata(self):
    if 'mesh_metadata' in self.info:
      return self.info["mesh_metadata"]
    return None

  @property
  def uniform_draco_grid_size(self):
    """
    If not None, a number that specifies the draco_grid_size at every ChunkedGraph level.
    """
    if self.mesh_metadata and 'uniform_draco_grid_size' in self.mesh_metadata:
      return self.mesh_metadata["uniform_draco_grid_size"]
    return None

  @property
  def max_meshed_layer(self):
    """
    The highest level in the ChunkedGraph that we create meshes for in this dataset.
    """
    if self.mesh_metadata and 'max_meshed_layer' in self.mesh_metadata:
      return self.mesh_metadata["max_meshed_layer"]
    return None

  def get_draco_grid_size(self, level):
    """
    Returns the draco_grid_size for specified ChunkedGraph level.
    """
    if self.mesh_metadata is None:
      raise ValueError('This layer is not draco meshed')
    if self.uniform_draco_grid_size is not None:
      return self.uniform_draco_grid_size
    if self.mesh_metadata["max_meshed_layer"] < level:
      raise ValueError(
        "Request level",
        level,
        ". But the maximum meshed level is ",
        self.mesh_metadata["max_meshed_layer"],
      )
    return self.mesh_metadata["draco_grid_sizes"][str(level)]

GraphenePath = namedtuple('GraphenePath', ('scheme', 'subdomain', 'domain', 'modality', 'version', 'dataset'))
LEGACY_EXTRACTION_RE = re.compile(r'/?(\w+)/([\d\.]+)/([\w\d\.\_\-]+)/?')
API_VX_EXTRACTION_RE = re.compile(r'/?(\w+)/api/(v[\d\.]+)/([\w\d\.\_\-]+)/?')
LATEST_API_EXTRACTION_RE = re.compile(r'/?(\w+)/(table)/([\w\d\.\_\-]+)/?')

def extract_graphene_path(url):
  """
  Examples:
  Legacy endpoint:
    graphene://https://SUBDOMAIN.dynamicannotationframework.com/segmentation/1.0/DATASET
  Newer endpoint:
    graphene://https://SUBDOMAIN.dynamicannotationframework.com/segmentation/api/v1/DATASET
  Latest endpoint:
    graphene://https://SUBDOMAIN.DOMAIN_DOT_COM/segmentation/table/DATASET
  """
  parse = urllib.parse.urlparse(url)
  subdomain = parse.netloc.split('.')[0]
  domain = '.'.join(parse.netloc.split('.')[1:])

  schemes = [ 
    LATEST_API_EXTRACTION_RE, API_VX_EXTRACTION_RE, LEGACY_EXTRACTION_RE 
  ]

  for scheme in schemes:
    match = re.match(scheme, parse.path)
    if match:
      break
  else:
    raise exceptions.UnsupportedFormatError("Unable to parse Graphene URL: " + url)

  modality, version, dataset = match.groups()
  return GraphenePath(parse.scheme, subdomain, domain, modality, version, dataset)

