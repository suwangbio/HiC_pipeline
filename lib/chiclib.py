# Created on Mon May 25 16:57:52 2015

# Author: XiaoTao Wang
# Organization: HuaZhong Agricultural University

import logging, os, time
import numpy as np
from mirnylib.genome import Genome
from hiclib.fragmentHiC import HiCdataset
from mirnylib.numutils import uniqueIndex, fillDiagonal
from mirnylib.h5dict import h5dict

log = logging.getLogger(__name__)

# A Customized HiCdataset Class
class cHiCdataset(HiCdataset):
    
    def parseInputData(self, dictLike, zeroBaseChrom=True, **kwargs):
        
        import numexpr
        
        if not os.path.exists(dictLike):
            raise IOError('File not found: %s' % dictLike)
        
        dictLike = h5dict(dictLike, 'r')
        
        a = dictLike["chrms1"]
        self.trackLen = len(a)

        if zeroBaseChrom == True:
            self.chrms1 = a
            self.chrms2 = dictLike["chrms2"]
        else:
            self.chrms1 = a - 1
            self.chrms2 = dictLike["chrms2"] - 1
        self.N = len(self.chrms1)

        del a

        self.cuts1 = dictLike['cuts1']
        self.cuts2 = dictLike['cuts2']
        
        self.strands1 = dictLike["strands1"]
        self.strands2 = dictLike["strands2"]

        self.metadata['010_CheckConsistency'] = self.trackLen

        try:
            dictLike['misc']['genome']['idx2label']
            self.updateGenome(self.genome,
                              oldGenome=dictLike["misc"]["genome"]["idx2label"],
                              putMetadata=True)
        except KeyError:
            assumedGenome = Genome(self.genome.genomePath)
            self.updateGenome(self.genome, oldGenome=assumedGenome, putMetadata=True)

        # Discard dangling ends and self-circles
        DSmask = (self.chrms1 >= 0) * (self.chrms2 >= 0)
        self.metadata['100_NormalPairs'] = DSmask.sum()

        sameFragMask = self.evaluate("a = (fragids1 == fragids2)",
                     ["fragids1", "fragids2"]) * DSmask

        cutDifs = self.cuts2[sameFragMask] > self.cuts1[sameFragMask]
        s1 = self.strands1[sameFragMask]
        s2 = self.strands2[sameFragMask]
        SSDE = (s1 != s2)
        SS = SSDE * (cutDifs == s2)
        SS_N = SS.sum()
        SSDE_N = SSDE.sum()
        sameFrag_N = sameFragMask.sum()
        self.metadata['120_SameFragmentReads'] = sameFrag_N
        self.metadata['122_SelfLigationReads'] = SS_N
        self.metadata['124_DanglingReads'] = SSDE_N - SS_N
        self.metadata['126_UnknownMechanism'] = sameFrag_N - SSDE_N
        
        mask = DSmask * (-sameFragMask)

        del DSmask, sameFragMask
        
        noSameFrag = mask.sum()
        
        # distance between sites facing each other
        dist = self.evaluate("a = numexpr.evaluate('- cuts1 * (2 * strands1 -1) - "
                             "cuts2 * (2 * strands2 - 1)')",
                             ["cuts1", "cuts2", "strands1", "strands2"],
                             constants={"numexpr":numexpr})

        readsMolecules = self.evaluate(
            "a = numexpr.evaluate('(chrms1 == chrms2) & (strands1 != strands2) &  (dist >=0) &"
            " (dist <= maximumMoleculeLength)')",
            internalVariables=["chrms1", "chrms2", "strands1", "strands2"],
            externalVariables={"dist": dist},
            constants={"maximumMoleculeLength": self.maximumMoleculeLength, "numexpr": numexpr})

        mask *= (readsMolecules == False)
        extraDE = mask.sum()
        self.metadata['210_ExtraDanglingReads'] = -extraDE + noSameFrag
        if mask.sum() == 0:
            raise Exception('No reads left after filtering. Please, check the input data')

        del dist, readsMolecules
        
        del dictLike
    
    def filterDuplicates(self):

        Nds = self.N

        # an array to determine unique rows. Eats 16 bytes per DS record
        dups = np.zeros((Nds, 2), dtype="int64", order="C")

        dups[:, 0] = self.chrms1
        dups[:, 0] *= self.fragIDmult
        dups[:, 0] += self.cuts1
        dups[:, 1] = self.chrms2
        dups[:, 1] *= self.fragIDmult
        dups[:, 1] += self.cuts2
        dups.sort(axis=1)
        dups.shape = (Nds * 2)
        strings = dups.view("|S16")
            # Converting two indices to a single string to run unique
        uids = uniqueIndex(strings)
        del strings, dups
        stay = np.zeros(Nds, bool)
        stay[uids] = True  # indexes of unique DS elements
        del uids
        uflen = len(self.ufragments)
        self.metadata["310_DuplicatedRemoved"] = len(stay) - stay.sum()
        self.maskFilter(stay)
        assert len(self.ufragments) == uflen  # self-check
    
    def filterRsiteStart(self, offset=5):

        expression = "mask = (np.abs(dists1 - fraglens1) >= offset) * "\
        "((np.abs(dists2 - fraglens2) >= offset) )"
        
        mask = self.evaluate(expression,
                             internalVariables=["dists1", "fraglens1",
                                                "dists2", "fraglens2"],
                             constants={"offset": offset, "np": np},
                             outVariable=("mask", np.zeros(self.N, bool)))
                             
        self.metadata["320_StartNearRsiteReads"] = len(mask) - mask.sum()
        self.maskFilter(mask)
    
    def merge(self, filenames):

        h5dicts = [h5dict(i, mode = 'r') for i in filenames]
        
        if all(["metadata" in i for i in h5dicts]):
            metadatas = [mydict["metadata"] for mydict in h5dicts]
            # print metadatas
            newMetadata = metadatas.pop()
            for oldData in metadatas:
                for key, value in oldData.items():
                    if (key in newMetadata):
                        newMetadata[key] += value
                    else:
                        log.warning('The key %s can not be found in some files',
                                    key)
            self.metadata = newMetadata
            self.h5dict["metadata"] = self.metadata

        for name in self.vectors.keys():
            res = []
            for mydict in h5dicts:
                res.append(mydict[name])
            res = np.concatenate(res)
            self.N = len(res)
            self.DSnum = self.N
            self._setData(name, res)
            self.h5dict.flush()
            time.sleep(0.2)  # allow buffers to flush
        
        LeftType = np.zeros(50, dtype = int)
        RightType = np.zeros(50, dtype = int)
        InnerType = np.zeros(50, dtype = int)
        OuterType = np.zeros(50, dtype = int)
        for mydict in h5dicts:
            LeftType += mydict['LeftType']
            RightType += mydict['RightType']
            InnerType += mydict['InnerType']
            OuterType += mydict['OuterType']
        
        self.h5dict['LeftType'] = LeftType
        self.h5dict['RightType'] = RightType
        self.h5dict['InnerType'] = InnerType
        self.h5dict['OuterType'] = OuterType
        
        self.rebuildFragments()
    
    def printMetadata(self, saveTo=None):
        self._dumpMetadata()
        for i in sorted(self.metadata):
            if (i[2] != '0'):
                print '\t\t',
            elif (i[1] != '0') and (i[2] == '0'):
                print '\t',
            print i, self.metadata[i]
        if saveTo != None:
            with open(saveTo, 'w') as myfile:
                for i in sorted(self.metadata):
                    if (i[2] != '0'):
                        myfile.write('\t\t')
                    elif (i[1] != '0') and (i[2] == '0'):
                        myfile.write('\t')
                    myfile.write(str(i))
                    myfile.write(':   ')
                    myfile.write(str(self.metadata[i]))
                    myfile.write('\n')
    
    def saveByChromosomeHeatmap(self, filename, resolution = 40000,
                                includeTrans = False,
                                countDiagonalReads = "Once"):
        """
        Saves chromosome by chromosome heatmaps to h5dict.
        
        This method is not as memory demanding as saving all x all heatmap.

        Keys of the h5dict are of the format ["1 1"], where chromosomes are
        zero-based, and there is one space between numbers.

        Parameters
        ----------
        filename : str
            Filename of the h5dict with the output
            
        resolution : int
            Resolution to save heatmaps
            
        includeTrans : bool, optional
            Build inter-chromosomal heatmaps (default: False)
            
        countDiagonalReads : "once" or "twice"
            How many times to count reads in the diagonal bin

        """
        if countDiagonalReads.lower() not in ["once", "twice"]:
            raise ValueError("Bad value for countDiagonalReads")
            
        self.genome.setResolution(resolution)
        
        pos1 = self.evaluate("a = np.array(mids1 / {res}, dtype = 'int32')"
                             .format(res=resolution), "mids1")
        pos2 = self.evaluate("a = np.array(mids2 / {res}, dtype = 'int32')"
                             .format(res=resolution), "mids2")
                             
        chr1 = self.chrms1
        chr2 = self.chrms2
        
        # DS = self.DS  # 13 bytes per read up to now, 16 total
        mydict = h5dict(filename)

        for chrom in xrange(self.genome.chrmCount):
            if includeTrans == True:
                mask = ((chr1 == chrom) + (chr2 == chrom))
            else:
                mask = ((chr1 == chrom) * (chr2 == chrom))
            # Located chromosomes and positions of chromosomes
            c1, c2, p1, p2 = chr1[mask], chr2[mask], pos1[mask], pos2[mask]
            if includeTrans == True:
                # moving different chromosomes to c2
                # c1 == chrom now
                mask = (c2 == chrom) * (c1 != chrom)
                c1[mask], c2[mask], p1[mask], p2[mask] = c2[mask].copy(), c1[
                    mask].copy(), p2[mask].copy(), p1[mask].copy()
                del c1  # ignore c1
                args = np.argsort(c2)
                c2 = c2[args]
                p1 = p1[args]
                p2 = p2[args]

            for chrom2 in xrange(chrom, self.genome.chrmCount):
                if (includeTrans == False) and (chrom2 != chrom):
                    continue
                start = np.searchsorted(c2, chrom2, "left")
                end = np.searchsorted(c2, chrom2, "right")
                cur1 = p1[start:end]
                cur2 = p2[start:end]
                label = np.asarray(cur1, "int64")
                label *= self.genome.chrmLensBin[chrom2]
                label += cur2
                maxLabel = self.genome.chrmLensBin[chrom] * \
                           self.genome.chrmLensBin[chrom2]
                counts = np.bincount(label, minlength = maxLabel)
                assert len(counts) == maxLabel
                mymap = counts.reshape((self.genome.chrmLensBin[chrom], -1))
                if chrom == chrom2:
                    mymap = mymap + mymap.T
                    if countDiagonalReads.lower() == "once":
                        fillDiagonal(mymap, np.diag(mymap).copy() / 2)
                mydict["%d %d" % (chrom, chrom2)] = mymap
        
        mydict['resolution'] = resolution

        return