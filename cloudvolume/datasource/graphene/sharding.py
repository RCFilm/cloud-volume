from ..precomputed.sharding import ShardingSpecification, ShardReader

class GrapheneShardReader(ShardReader):
  def compute_shard_location(self, label):
    shard_loc = self.spec.compute_shard_location(label)
    chunk_id = self.meta.meta.decode_chunk_id(label)
    filename = str(chunk_id) + '.shard'
    return (filename, shard_loc.minishard_number)


 