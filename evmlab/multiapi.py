
import shelve

class MultiApi(object):

    """ Helper class for using several API:s. 
    Currently supports web3 and etherchain

    web3 is a bit better, since it allows for querying about balance
    and things at specified block height.
    """

    def __init__(self, web3 = None, etherchain = None):
        self.web3 = web3
        self.etherchain = etherchain

    def _getCached(self,key):
        db = shelve.open(".api_cache")
        obj = None
        if key in db:
            obj = db[key]
        db.close()
        return obj

    def _putCached(self,key, obj):
        db = shelve.open(".api_cache")
        db[key] = obj
        db.close()

    def getAccountInfo(self, address, blnum = None):
        acc = {}

        if blnum is not None: 
            cachekey = "%s-%d" % (address, blnum)
            cached = self._getCached(cachekey)
            if cached is not None:
                return cached

        if self.web3 is not None: 
            acc['balance'] = self.web3.eth.getBalance(address, blnum)
            acc['code']    = self.web3.eth.getCode(address, blnum)
            acc['nonce']   = self.web3.eth.getTransactionCount(address, blnum)
            acc['address'] = address

            # cache it, but only if it's at a specific block number
            if blnum is not None:
                self._putCached(cachekey, acc)

        elif self.etherchain is not None: 
            acc = self.etherchain.getAccount(address)

        return acc

    def getTransaction(self,h):

        cachekey = "tx-%s" % h

        o = self._getCached(cachekey)
        if o is not None:
            return o

        translations = [("sender", "from"),
                        ("recipient", "to"),
                        ("block_id", "blockNumber" )]

        if self.web3 : 
            obj = self.web3.eth.getTransaction(h)
            obj_dict = {}
            for a in obj:
              obj_dict[a] = obj[a]
            for (a,b) in translations:
                obj_dict[a] = obj_dict[b]

        else:
            obj = self.etherchain.getTransaction(h)
            obj_dict = {key: value for (key, value) in obj}
            for (a,b) in translations:
                obj_dict[b] = obj_dict[a]

        self._putCached( cachekey, obj_dict)
        return obj_dict

    def getStorageSlot(self, addr, key, blnum = None):

        if blnum is not None: 
            cachekey = "%s-%d-%s" % (addr,blnum,key)
            cached = self._getCached(cachekey)
            if cached is not None:
                return cached

        if self.web3:
            try:
                value = self.web3.eth.getStorageAt(addr, key, blnum)
                self._putCached(cachekey, value)
                return value
            except Exception as e:
                    print(e)
                    return ""
            
        else:
            print("getStorageSlot not implemented for etherchain api")
            return ""